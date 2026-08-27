"""
微信读书扫码登录代理服务。

主仓界面（首页「微信续期」/设置页「认证资产」）把微信读书扫码登录透传到
外部工具 wechat-mp-tools（http://127.0.0.1:5300）的 auth API，本模块负责代理调用。

外部工具地址 SSOT：config/rss_settings.json -> wechat_tool.base_url
（由 apps/subscriptions/rss_config.load_settings 补默认值）。

外部工具接口（来自 wechat-mp-tools backend/auth.py / account_pool_api.py）：
- POST /api/auth/login         触发扫码，返回 {"message": ...}
- GET  /api/auth/status        登录状态聚合，见 get_login_status 归一化
- POST /api/auth/cancel        取消扫码
- GET  /api/account-pool       脱敏账号列表（含 remaining_seconds/expired/status/nickname）

本模块均为同步函数：视图渲染路径（views.py）直接调用；FastAPI 路由在 async 里用
run_in_threadpool 包装，避免阻塞事件循环。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
import threading

import httpx

from apps.subscriptions.rss_config import load_settings

# 外部工具不可达哨兵做磁盘持久化：外部工具是否在跑是慢变状态，进程重启后
# 冷首访也可能命中上次探测结果，避免每次冷启动都付 4s 探测超时。
_UNAVAILABLE_MARKER = Path(__file__).resolve().parents[2] / "runtime" / "weread_login_unavailable.json"
# 外部工具是否在跑是低频变化的手动启停状态。不可达哨兵放长持久化 TTL,
# 让日常冷启动设置页也能命中,避免每次冷起都付约 4s 的探测超时。
# 某次真实探测可达时哨兵会被覆盖清除,恢复实时。
_UNAVAILABLE_PERSIST_TTL = 3600.0
_cache_lock = threading.Lock()


def _tool_base_url() -> str:
    settings = load_settings()
    return str((settings.get("wechat_tool") or {}).get("base_url") or "").rstrip("/")


def _request(method: str, path: str, timeout: float = 8.0) -> Dict[str, Any] | None:
    """调用外部工具接口，返回 JSON 字典；不可达/非 JSON 返回 None。"""
    base = _tool_base_url()
    if not base:
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, f"{base}{path}")
        if resp.status_code >= 400:
            return None
        return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else None
    except Exception:
        return None


def _fmt_remaining(seconds: int) -> str:
    seconds = max(int(seconds or 0), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def _fmt_expire_time(seconds: int) -> str:
    if not seconds:
        return ""
    return datetime.fromtimestamp(int(seconds)).strftime("%Y/%m/%d %H:%M")


_LOGIN_STATUS_UNAVAILABLE_CACHE: tuple[float, Dict[str, Any]] | None = None
# 外部工具不可达时会阻塞到超时（约 8s×2 次请求）。AVent 仅在该场景下生效，
# 缓存"不可达"哨兵，TTL 内页面渲染直接复用，避免每次渲染都慢好几秒。
_LOGIN_STATUS_UNAVAILABLE_TTL = 10.0


def _load_persisted_unavailable() -> tuple[float, Dict[str, Any]] | None:
    """恢复上次持久化的不可达哨兵(跨进程冷启动命中)。"""
    try:
        data = json.loads(_UNAVAILABLE_MARKER.read_text(encoding="utf-8"))
        ts = float(data.get("timestamp") or 0)
        payload = data.get("payload")
        if ts and isinstance(payload, dict):
            return (ts, payload)
    except Exception:
        pass
    return None


def _persist_unavailable(payload: Dict[str, Any]) -> None:
    try:
        _UNAVAILABLE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _UNAVAILABLE_MARKER.write_text(
            json.dumps({"timestamp": time.time(), "payload": payload}),
            encoding="utf-8",
        )
    except Exception:
        pass


def get_login_status_cached() -> Dict[str, Any]:
    """主界面用的非阻塞读取：有上次"不可达"缓存则立刻返回，无缓存则返回默认态。

    微信续期按钮态是次要提示，不该用它阻塞页面首载（外部工具不可达时会超时）。
    """
    if _LOGIN_STATUS_UNAVAILABLE_CACHE is not None:
        return _LOGIN_STATUS_UNAVAILABLE_CACHE[1]
    return {
        "is_available": False,
        "is_expired": False,
        "is_expiring_soon": False,
        "remaining_text": "",
        "expire_time_text": "",
        "nickname": "",
        "status_text": "",
        "login_state": {},
        "logged_in": False,
        "message": "",
        "last_error": "",
    }


def get_login_status(*, cache_unavailable: bool = False) -> Dict[str, Any]:
    """
    归一化外部工具登录状态为兼容 views.py wechat_auth 结构 + 扫码状态。

    返回字段（对齐 connectors/wechat/auth.py 的 get_wechat_auth_status）：
      is_available / is_expired / is_expiring_soon / remaining_text /
      expire_time_text / nickname / status_text / login_state / logged_in / message

    视图渲染路径（views.py）传 cache_unavailable=True：仅当外部工具不可达时缓存
    结果，工具可达时始终实时请求（避免扫码登录后读到旧状态）。扫码轮询路由保持
    默认，保证每 2 秒轮询拿到最新扫码状态。
    """
    global _LOGIN_STATUS_UNAVAILABLE_CACHE
    now = time.time()

    if cache_unavailable and _LOGIN_STATUS_UNAVAILABLE_CACHE is not None:
        if (now - _LOGIN_STATUS_UNAVAILABLE_CACHE[0]) < _LOGIN_STATUS_UNAVAILABLE_TTL:
            return _LOGIN_STATUS_UNAVAILABLE_CACHE[1]

    # 冷启动(缓存空)时,先尝试命中持久化的不可达哨兵,避免付探测超时。
    # 磁盘哨兵已用 300s TTL 校验新鲜度,水合进内存时应以"此刻"为时间戳,
    # 否则旧的写入时间会立刻被 10s 内存 TTL 判失效,导致仍去真探测。
    if cache_unavailable and _LOGIN_STATUS_UNAVAILABLE_CACHE is None:
        with _cache_lock:
            persisted = _load_persisted_unavailable()
        if persisted is not None and (now - persisted[0]) < _UNAVAILABLE_PERSIST_TTL:
            _LOGIN_STATUS_UNAVAILABLE_CACHE = (now, persisted[1])
            return persisted[1]

    result = _request("GET", "/api/auth/status")
    pool = _request("GET", "/api/account-pool")

    if result is None:
        payload = {
            "is_available": False,
            "is_expired": True,
            "is_expiring_soon": False,
            "remaining_text": "",
            "expire_time_text": "",
            "nickname": "",
            "status_text": "外部工具未运行",
            "login_state": {"status": "idle", "message": "外部工具未运行", "qrcode": "", "uuid": ""},
            "logged_in": False,
            "message": "外部工具未运行（127.0.0.1:5300 不可达）",
        }
        # 外部工具不可达时，views 路径缓存该哨兵，TTL 内复用，避免每个页面渲染都阻塞超时
        if cache_unavailable:
            _LOGIN_STATUS_UNAVAILABLE_CACHE = (now, payload)
            with _cache_lock:
                _persist_unavailable(payload)
        return payload

    # 账号池：取第一个 active 账号（微信读书 token 载体）
    account = None
    for acc in (pool or {}).get("accounts", []) or []:
        if acc.get("status") == "active":
            account = acc
            break
    account = account or ((pool or {}).get("accounts", []) or [None])[0]

    logged_in = bool(result.get("logged_in")) and bool(account)
    login_state = result.get("login_state") or {}
    status_text = "登录有效" if logged_in else ("未登录" if result.get("logged_in") is False else "登录失效")

    remaining = int(account.get("remaining_seconds") or 0) if account else 0
    expired = bool(account.get("expired")) if account else True
    save_time = int(account.get("save_time") or 0) if account else 0
    expire_ts = save_time + remaining if save_time else 0
    nickname = str((account or {}).get("nickname") or "") if account else ""
    last_error = str((account or {}).get("last_error") or "") if account else ""

    is_expired = bool(result.get("logged_in")) is False or expired or not account
    is_expiring_soon = bool(remaining) and remaining <= 24 * 3600 and not is_expired

    result = {
        "is_available": logged_in,
        "is_expired": is_expired,
        "is_expiring_soon": is_expiring_soon,
        "remaining_text": _fmt_remaining(remaining) if remaining and not expired else "",
        "expire_time_text": _fmt_expire_time(expire_ts) if expire_ts else "",
        "nickname": nickname,
        "status_text": status_text,
        "login_state": login_state,
        "logged_in": logged_in,
        "message": login_state.get("message") or status_text,
        "last_error": last_error,
    }
    return result


def start_login() -> Dict[str, Any]:
    """触发外部工具扫码登录，返回 {"ok": bool, "message": str}。"""
    payload = _request("POST", "/api/auth/login")
    if payload is None:
        return {"ok": False, "message": "外部工具未运行（127.0.0.1:5300 不可达）"}
    return {"ok": True, "message": str(payload.get("message") or "已请求微信读书扫码二维码")}


def cancel_login() -> Dict[str, Any]:
    """取消外部工具扫码登录。"""
    payload = _request("POST", "/api/auth/cancel")
    return {"ok": payload is not None, "message": "已取消" if payload is not None else "外部工具未运行"}

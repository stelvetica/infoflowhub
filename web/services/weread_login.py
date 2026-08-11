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

from datetime import datetime
from typing import Any, Dict

import httpx

from apps.subscriptions.rss_config import load_settings


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


def get_login_status() -> Dict[str, Any]:
    """
    归一化外部工具登录状态为兼容 views.py wechat_auth 结构 + 扫码状态。

    返回字段（对齐 connectors/wechat/auth.py 的 get_wechat_auth_status）：
      is_available / is_expired / is_expiring_soon / remaining_text /
      expire_time_text / nickname / status_text / login_state / logged_in / message
    """
    status = _request("GET", "/api/auth/status")
    pool = _request("GET", "/api/account-pool")

    if status is None:
        return {
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

    # 账号池：取第一个 active 账号（微信读书 token 载体）
    account = None
    for acc in (pool or {}).get("accounts", []) or []:
        if acc.get("status") == "active":
            account = acc
            break
    account = account or ((pool or {}).get("accounts", []) or [None])[0]

    logged_in = bool(status.get("logged_in")) and bool(account)
    login_state = status.get("login_state") or {}
    status_text = "登录有效" if logged_in else ("未登录" if status.get("logged_in") is False else "登录失效")

    remaining = int(account.get("remaining_seconds") or 0) if account else 0
    expired = bool(account.get("expired")) if account else True
    save_time = int(account.get("save_time") or 0) if account else 0
    expire_ts = save_time + remaining if save_time else 0
    nickname = str((account or {}).get("nickname") or "") if account else ""
    last_error = str((account or {}).get("last_error") or "") if account else ""

    is_expired = bool(status.get("logged_in")) is False or expired or not account
    is_expiring_soon = bool(remaining) and remaining <= 24 * 3600 and not is_expired

    return {
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

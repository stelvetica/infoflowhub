from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_AUTH_PATH = BASE_DIR / "runtime" / "wechat_auth.json"


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def extract_wechat_fakeid(source: dict) -> str:
    feed_url = _clean_text(source.get("feed_url"))
    site_url = _clean_text(source.get("site_url"))
    for value in (feed_url, site_url):
        if value.startswith("wechat://mp/"):
            return value.split("wechat://mp/", 1)[1].strip().strip("/")
    return ""


def load_wechat_credentials() -> dict[str, object]:
    env_credentials = {
        "token": _clean_text(os.getenv("WECHAT_TOKEN")),
        "cookie": _clean_text(os.getenv("WECHAT_COOKIE")),
        "fakeid": _clean_text(os.getenv("WECHAT_FAKEID")),
        "nickname": _clean_text(os.getenv("WECHAT_NICKNAME")),
        "expire_time": _clean_text(os.getenv("WECHAT_EXPIRE_TIME")),
    }
    if env_credentials["token"] and env_credentials["cookie"]:
        return env_credentials
    try:
        payload = json.loads(RUNTIME_AUTH_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {
                "token": _clean_text(payload.get("token")),
                "cookie": _clean_text(payload.get("cookie")),
                "fakeid": _clean_text(payload.get("fakeid")),
                "nickname": _clean_text(payload.get("nickname")),
                "expire_time": _clean_text(payload.get("expire_time")),
            }
    except Exception:
        pass
    return env_credentials


def validate_wechat_auth_prerequisite(source: dict) -> str:
    credentials = load_wechat_credentials()
    token = _clean_text(credentials.get("token"))
    cookie = _clean_text(credentials.get("cookie"))
    if not token or not cookie:
        return (
            "微信公众号抓取依赖本地登录态。"
            "请先在环境变量或 runtime/wechat_auth.json 中配置 WECHAT_TOKEN 与 WECHAT_COOKIE。"
        )
    expire_raw = _clean_text(credentials.get("expire_time"))
    if expire_raw.isdigit():
        expire_time = int(expire_raw)
        now_ms = int(time.time() * 1000)
        if expire_time > 0 and now_ms > expire_time:
            return "微信公众号登录态已过期，请更新 WECHAT_TOKEN / WECHAT_COOKIE 后重试。"
    return ""


def get_wechat_auth_status() -> dict[str, Any]:
    credentials = load_wechat_credentials()
    token = _clean_text(credentials.get("token"))
    cookie = _clean_text(credentials.get("cookie"))
    nickname = _clean_text(credentials.get("nickname"))
    expire_raw = _clean_text(credentials.get("expire_time"))
    now_ms = int(time.time() * 1000)
    expire_time = int(expire_raw) if expire_raw.isdigit() else 0
    has_credentials = bool(token and cookie)
    is_expired = bool(expire_time and now_ms > expire_time)
    is_expiring_soon = bool(expire_time and not is_expired and (expire_time - now_ms) <= 24 * 3600 * 1000)
    remaining_hours = max(int((expire_time - now_ms) / 3600000), 0) if expire_time else -1
    if not has_credentials:
        return {
            "has_credentials": False,
            "is_expired": False,
            "is_expiring_soon": False,
            "nickname": nickname,
            "expire_time": expire_time,
            "remaining_hours": remaining_hours,
            "status_text": "未登录",
            "status_level": "warn",
            "hint": "请扫码登录一次，系统会把公众号登录态持久化到本地运行文件。",
        }
    if is_expired:
        return {
            "has_credentials": True,
            "is_expired": True,
            "is_expiring_soon": False,
            "nickname": nickname,
            "expire_time": expire_time,
            "remaining_hours": remaining_hours,
            "status_text": "已过期",
            "status_level": "danger",
            "hint": "当前公众号登录态已过期，点击续期后重新扫码即可恢复。",
        }
    if is_expiring_soon:
        return {
            "has_credentials": True,
            "is_expired": False,
            "is_expiring_soon": True,
            "nickname": nickname,
            "expire_time": expire_time,
            "remaining_hours": remaining_hours,
            "status_text": f"即将过期（约 {remaining_hours} 小时）",
            "status_level": "warn",
            "hint": "当前公众号登录态接近过期，建议现在续期，避免下次抓取时失效。",
        }
    return {
        "has_credentials": True,
        "is_expired": False,
        "is_expiring_soon": False,
        "nickname": nickname,
        "expire_time": expire_time,
        "remaining_hours": remaining_hours,
        "status_text": "可用",
        "status_level": "ok",
        "hint": "当前公众号登录态可用；如果后续抓取报登录失效，再点续期重新扫码即可。",
    }

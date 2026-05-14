from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[3]
RUNTIME_DIR = BASE_DIR / "runtime"
AUTH_DIR = RUNTIME_DIR / "auth"
WECHAT_AUTH_PATH = AUTH_DIR / "wechat_mp_main.json"
LEGACY_WECHAT_AUTH_PATH = RUNTIME_DIR / "wechat_auth.json"


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def get_wechat_credentials() -> dict[str, object]:
    env_credentials = {
        "token": _clean_text(os.getenv("WECHAT_TOKEN")),
        "cookie": _clean_text(os.getenv("WECHAT_COOKIE")),
        "fakeid": _clean_text(os.getenv("WECHAT_FAKEID")),
        "nickname": _clean_text(os.getenv("WECHAT_NICKNAME")),
        "expire_time": _clean_text(os.getenv("WECHAT_EXPIRE_TIME")),
    }
    if env_credentials["token"] and env_credentials["cookie"]:
        return env_credentials
    for path in (WECHAT_AUTH_PATH, LEGACY_WECHAT_AUTH_PATH):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            return {
                "token": _clean_text(payload.get("token")),
                "cookie": _clean_text(payload.get("cookie")),
                "fakeid": _clean_text(payload.get("fakeid")),
                "nickname": _clean_text(payload.get("nickname")),
                "expire_time": _clean_text(payload.get("expire_time")),
            }
    return env_credentials


def save_wechat_credentials(credentials: dict[str, object]) -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(credentials, ensure_ascii=False, indent=2) + "\n"
    WECHAT_AUTH_PATH.write_text(payload, encoding="utf-8")
    LEGACY_WECHAT_AUTH_PATH.write_text(payload, encoding="utf-8")


def validate_wechat_auth() -> dict[str, object]:
    credentials = get_wechat_credentials()
    token = _clean_text(credentials.get("token"))
    cookie = _clean_text(credentials.get("cookie"))
    expire_raw = _clean_text(credentials.get("expire_time"))
    now_ms = int(time.time() * 1000)
    expire_time = int(expire_raw) if expire_raw.isdigit() else 0
    if not token or not cookie:
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": "请扫码登录一次，系统会把公众号登录态持久化到本地认证文件。",
            "remaining_hours": -1,
            "is_expired": False,
            "is_expiring_soon": False,
        }
    if expire_time and now_ms > expire_time:
        return {
            "is_available": True,
            "status_level": "danger",
            "status_text": "已过期",
            "hint": "当前公众号登录态已过期，点击续期后重新扫码即可恢复。",
            "remaining_hours": 0,
            "is_expired": True,
            "is_expiring_soon": False,
        }
    if expire_time and (expire_time - now_ms) <= 24 * 3600 * 1000:
        remaining_hours = max(int((expire_time - now_ms) / 3600000), 0)
        return {
            "is_available": True,
            "status_level": "warn",
            "status_text": f"即将过期（约 {remaining_hours} 小时）",
            "hint": "当前公众号登录态接近过期，建议现在续期，避免下次抓取时失效。",
            "remaining_hours": remaining_hours,
            "is_expired": False,
            "is_expiring_soon": True,
        }
    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用",
        "hint": "当前公众号登录态可用；如果后续抓取报登录失效，再点续期重新扫码即可。",
        "remaining_hours": -1,
        "is_expired": False,
        "is_expiring_soon": False,
    }


def get_wechat_status() -> dict[str, Any]:
    credentials = get_wechat_credentials()
    status = validate_wechat_auth()
    return {
        "has_credentials": bool(_clean_text(credentials.get("token")) and _clean_text(credentials.get("cookie"))),
        "nickname": _clean_text(credentials.get("nickname")),
        "expire_time": int(_clean_text(credentials.get("expire_time"))) if _clean_text(credentials.get("expire_time")).isdigit() else 0,
        **status,
    }

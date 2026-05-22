from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from infra.text_normalizer import clean_text, normalize_utf8_text
from infra.utf8_json import dump_json_utf8, load_json_utf8


BASE_DIR = Path(__file__).resolve().parents[3]
RUNTIME_DIR = BASE_DIR / "runtime"
AUTH_DIR = RUNTIME_DIR / "auth"
WECHAT_AUTH_PATH = AUTH_DIR / "wechat_mp_main.json"
LEGACY_WECHAT_AUTH_PATH = RUNTIME_DIR / "wechat_auth.json"
WEB_LOG_PATH = RUNTIME_DIR / "web.log"
WECHAT_AUTH_EXPIRE_WINDOW_MS = 24 * 3600 * 1000


def _clean_text(value: object) -> str:
    return clean_text(value)


def _format_local_datetime(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M")


def _format_remaining_text(remaining_ms: int) -> str:
    if remaining_ms <= 0:
        return "0 分钟"
    total_minutes = max((remaining_ms + 59999) // 60000, 1)
    days, remain_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remain_minutes, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes and not days:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts[:2]) or "不足 1 分钟"


def log_wechat_auth_event(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        WEB_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with WEB_LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(f"[{timestamp}] [wechat_auth] {normalize_utf8_text(message)}\n")
    except Exception:
        return


def _extract_credentials(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    token = _clean_text(payload.get("token"))
    cookie = _clean_text(payload.get("cookie"))
    if not token or not cookie:
        return {}
    return {
        "token": token,
        "cookie": cookie,
        "fakeid": _clean_text(payload.get("fakeid")),
        "nickname": normalize_utf8_text(payload.get("nickname")),
        "expire_time": _clean_text(payload.get("expire_time")),
    }


def _load_credentials_from_file(path: Path) -> dict[str, object]:
    try:
        return _extract_credentials(load_json_utf8(path))
    except Exception:
        return {}


def _persist_canonical_credentials(credentials: dict[str, object]) -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    dump_json_utf8(WECHAT_AUTH_PATH, credentials)


def get_wechat_credentials() -> dict[str, object]:
    env_credentials = {
        "token": _clean_text(os.getenv("WECHAT_TOKEN")),
        "cookie": _clean_text(os.getenv("WECHAT_COOKIE")),
        "fakeid": _clean_text(os.getenv("WECHAT_FAKEID")),
        "nickname": normalize_utf8_text(os.getenv("WECHAT_NICKNAME")),
        "expire_time": _clean_text(os.getenv("WECHAT_EXPIRE_TIME")),
    }
    if env_credentials["token"] and env_credentials["cookie"]:
        return env_credentials
    canonical_credentials = _load_credentials_from_file(WECHAT_AUTH_PATH)
    if canonical_credentials:
        return canonical_credentials

    legacy_credentials = _load_credentials_from_file(LEGACY_WECHAT_AUTH_PATH)
    if legacy_credentials:
        _persist_canonical_credentials(legacy_credentials)
        log_wechat_auth_event(f"检测到 legacy 微信认证文件，已迁移到 canonical 存储：{WECHAT_AUTH_PATH}")
        return legacy_credentials
    return env_credentials


def save_wechat_credentials(credentials: dict[str, object]) -> None:
    payload = dict(credentials)
    payload["nickname"] = normalize_utf8_text(payload.get("nickname"))
    _persist_canonical_credentials(payload)


def validate_wechat_auth() -> dict[str, object]:
    credentials = get_wechat_credentials()
    token = _clean_text(credentials.get("token"))
    cookie = _clean_text(credentials.get("cookie"))
    expire_raw = _clean_text(credentials.get("expire_time"))
    now_ms = int(time.time() * 1000)
    expire_time = int(expire_raw) if expire_raw.isdigit() else 0
    expire_display = _format_local_datetime(expire_time)

    if not token or not cookie:
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": "请扫码登录一次，系统会把公众号登录态持久化到本地认证文件。",
            "remaining_hours": -1,
            "remaining_text": "",
            "is_expired": False,
            "is_expiring_soon": False,
            "expire_time": expire_time,
            "expire_time_text": expire_display,
        }

    if expire_raw and not expire_raw.isdigit():
        log_wechat_auth_event(f"检测到无效 expire_time={expire_raw}，已按未知过期时间处理。")
        return {
            "is_available": True,
            "status_level": "warn",
            "status_text": "有效期未知",
            "hint": "本地已保存公众号登录态，但 expire_time 格式无效，建议重新续期一次。",
            "remaining_hours": -1,
            "remaining_text": "",
            "is_expired": False,
            "is_expiring_soon": False,
            "expire_time": 0,
            "expire_time_text": "",
        }

    if expire_time and now_ms > expire_time:
        log_wechat_auth_event(f"公众号登录态已过期，expire_time={expire_display or expire_time}。")
        return {
            "is_available": True,
            "status_level": "danger",
            "status_text": "已过期",
            "hint": "当前公众号登录态已过期，点击续期/登录后重新扫码即可恢复。",
            "remaining_hours": 0,
            "remaining_text": "已过期",
            "is_expired": True,
            "is_expiring_soon": False,
            "expire_time": expire_time,
            "expire_time_text": expire_display,
        }

    if expire_time and (expire_time - now_ms) <= WECHAT_AUTH_EXPIRE_WINDOW_MS:
        remaining_ms = expire_time - now_ms
        remaining_hours = max((remaining_ms + 3599999) // 3600000, 0)
        remaining_text = _format_remaining_text(remaining_ms)
        log_wechat_auth_event(f"公众号登录态将在 24 小时内过期，剩余 {remaining_text}，expire_time={expire_display}。")
        return {
            "is_available": True,
            "status_level": "warn",
            "status_text": f"将过期（剩余 {remaining_text}）",
            "hint": "当前公众号登录态接近过期，建议现在续期，避免下次抓取时失效。",
            "remaining_hours": remaining_hours,
            "remaining_text": remaining_text,
            "is_expired": False,
            "is_expiring_soon": True,
            "expire_time": expire_time,
            "expire_time_text": expire_display,
        }

    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用",
        "hint": "当前公众号登录态可用；如果后续抓取报登录失效，再点续期/登录重新扫码即可。",
        "remaining_hours": -1,
        "remaining_text": _format_remaining_text(expire_time - now_ms) if expire_time else "",
        "is_expired": False,
        "is_expiring_soon": False,
        "expire_time": expire_time,
        "expire_time_text": expire_display,
    }


def get_wechat_status() -> dict[str, Any]:
    credentials = get_wechat_credentials()
    status = validate_wechat_auth()
    return {
        "has_credentials": bool(_clean_text(credentials.get("token")) and _clean_text(credentials.get("cookie"))),
        "nickname": normalize_utf8_text(credentials.get("nickname")),
        "expire_time": int(_clean_text(credentials.get("expire_time"))) if _clean_text(credentials.get("expire_time")).isdigit() else 0,
        **status,
    }

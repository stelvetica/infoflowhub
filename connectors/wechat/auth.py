from __future__ import annotations

import json
import os
import time
from pathlib import Path


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

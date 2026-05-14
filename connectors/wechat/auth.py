from __future__ import annotations

from typing import Any

from connectors.auth.providers.wechat import get_wechat_credentials, get_wechat_status, validate_wechat_auth


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
    return get_wechat_credentials()


def validate_wechat_auth_prerequisite(source: dict) -> str:
    status = validate_wechat_auth()
    if status.get("is_available") and not status.get("is_expired"):
        return ""
    return str(status.get("hint") or "微信公众号登录态不可用")


def get_wechat_auth_status() -> dict[str, Any]:
    return get_wechat_status()

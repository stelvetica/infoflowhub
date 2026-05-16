from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from infra.utf8_json import dump_json_utf8, load_json_utf8


BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = BASE_DIR / "config"
SOURCES_PATH = CONFIG_DIR / "subscription_sources.json"
SETTINGS_PATH = CONFIG_DIR / "rss_settings.json"
REQUIRED_SOURCE_FIELDS = (
    "id",
    "name",
    "group",
    "feed_url",
    "site_url",
    "provider",
    "fetch_via",
    "kind",
    "enabled",
    "note",
    "channel",
    "auth_key",
    "fallback_mode",
)


def _normalize_source_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    return {field: item[field] for field in REQUIRED_SOURCE_FIELDS}


def _validate_sources_payload(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("subscription_sources.json 顶层必须是对象")
    rows = data.get("sources")
    if not isinstance(rows, list):
        raise ValueError("subscription_sources.json 必须包含 sources 数组")
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 个订阅源必须是对象")
        missing = [field for field in REQUIRED_SOURCE_FIELDS if field not in item]
        if missing:
            raise ValueError(f"第 {index + 1} 个订阅源缺少字段: {', '.join(missing)}")
        normalized.append(_normalize_source_payload(item))
    return normalized


def load_sources() -> List[Dict[str, Any]]:
    data = load_json_utf8(SOURCES_PATH)
    return _validate_sources_payload(data)


def save_sources(sources: List[Dict[str, Any]]) -> None:
    normalized = _validate_sources_payload({"sources": sources})
    dump_json_utf8(SOURCES_PATH, {"sources": normalized})


def load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {
            "rsshub": {
                "public_base": "https://rsshub.app",
                "self_hosted_base": "",
                "prefer_self_hosted": False,
            }
        }
    return load_json_utf8(SETTINGS_PATH)

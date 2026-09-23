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


# 浏览器抓取后端：copy = 复制常用 Chrome Default profile 到副本（历史默认，行为不变）；
# bsk = 用 bsk CLI 驱动用户自己已登录的 Chromium。可按 source id 覆盖，默认不动。
DEFAULT_BROWSER_BACKEND = "copy"
BROWSER_BACKENDS = ("copy", "bsk")
# request_help_timeout：疑似登录/验证码等人的判定上限，用于逐命令的看门狗。
DEFAULT_REQUEST_HELP_TIMEOUT_SECONDS = 30.0
# command_timeout：导航这类真正慢的命令交给 bsk 自身的 --timeout，必须远大于前者，
# 否则正常慢加载会被误判成「疑似登录阻塞」。
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0
BROWSER_TIMEOUT_BOUNDS = (5.0, 600.0)


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


def _bounded_seconds(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    low, high = BROWSER_TIMEOUT_BOUNDS
    return min(max(value, low), high)


def browser_backend_settings(settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """浏览器后端配置的唯一归一化入口（SSOT）：缺省/非法值一律回落默认，调用方不做判断。"""
    section = (settings if settings is not None else load_settings()).get("browser_backend")
    if not isinstance(section, dict):
        section = {}
    default = str(section.get("default") or DEFAULT_BROWSER_BACKEND).strip().lower()
    if default not in BROWSER_BACKENDS:
        default = DEFAULT_BROWSER_BACKEND
    raw_sources = section.get("sources")
    sources: Dict[str, str] = {}
    if isinstance(raw_sources, dict):
        for key, value in raw_sources.items():
            backend = str(value or "").strip().lower()
            if backend in BROWSER_BACKENDS:
                sources[str(key)] = backend
    return {
        "default": default,
        "sources": sources,
        "request_help_timeout": _bounded_seconds(
            section.get("request_help_timeout"), DEFAULT_REQUEST_HELP_TIMEOUT_SECONDS
        ),
        "command_timeout": _bounded_seconds(section.get("command_timeout"), DEFAULT_COMMAND_TIMEOUT_SECONDS),
    }


def browser_backend_for(source: Dict[str, Any] | str | None) -> str:
    """某个源该用哪个浏览器后端：按 source id 覆盖优先，其次全局默认。"""
    settings = browser_backend_settings()
    source_id = source if isinstance(source, str) else str((source or {}).get("id") or "")
    return settings["sources"].get(source_id, settings["default"])


def browser_request_help_timeout() -> float:
    return browser_backend_settings()["request_help_timeout"]


def browser_command_timeout() -> float:
    return browser_backend_settings()["command_timeout"]

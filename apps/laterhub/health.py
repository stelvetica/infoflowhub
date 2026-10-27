"""laterhub 各抓取源的运行健康（最近成功/失败/错误）。"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from apps.laterhub.config import RUNTIME_DIR
from infra.utf8_json import dump_json_utf8, load_json_utf8

HEALTH_PATH = RUNTIME_DIR / "health" / "laterhub_source_health.json"

# 抓取与人工触发可能并发写健康文件，读-改-写整体串行化。
_LOCK = threading.Lock()


def load_health() -> dict[str, dict[str, str]]:
    """返回内层 sources 映射；文件缺失/损坏返回空 dict。"""
    try:
        payload: Any = load_json_utf8(HEALTH_PATH)
    except Exception:
        return {}
    sources = payload.get("sources") if isinstance(payload, dict) else None
    return sources if isinstance(sources, dict) else {}


def _clean_error(value: str) -> str:
    return " ".join(str(value or "").split())[:320]


def record_result(source_id: str, *, ok: bool, error: str = "") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        sources = load_health()
        current = dict(sources.get(source_id) or {})
        current["last_checked_at"] = now
        if ok:
            current["last_success_at"] = now
            current["last_error"] = ""
            current.setdefault("last_failed_at", "")
        else:
            current["last_failed_at"] = now
            current["last_error"] = _clean_error(error)
            current.setdefault("last_success_at", "")
        sources[source_id] = {
            key: str(current.get(key) or "")
            for key in ("last_checked_at", "last_success_at", "last_failed_at", "last_error")
        }
        dump_json_utf8(HEALTH_PATH, {"sources": sources})

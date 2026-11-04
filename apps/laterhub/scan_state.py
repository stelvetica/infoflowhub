"""深度全量扫描的到期状态（每个源上次「滚到底」成功的时间）。

状态读不懂时一律判为到期：宁可多扫一次，也不能因为状态文件坏掉而长期漏扫。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from apps.laterhub.config import DEEP_SCAN_STATE_PATH
from infra.utf8_json import dump_json_utf8, load_json_utf8

LAST_SCAN_KEY = "last_deep_scan_at"


def _now() -> datetime:
    return datetime.now().astimezone()


def _load_state() -> dict:
    payload = load_json_utf8(DEEP_SCAN_STATE_PATH, default={})
    return payload if isinstance(payload, dict) else {}


def deep_scan_due(source_id: str, interval_days: int, *, now: datetime | None = None) -> bool:
    current = now or _now()
    entry = _load_state().get(source_id)
    if not isinstance(entry, dict):
        return True
    raw = entry.get(LAST_SCAN_KEY)
    if not isinstance(raw, str) or not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    # 手写/旧状态可能不带时区：按当前时区理解，避免与 aware 的 now 比较直接抛 TypeError。
    if last.tzinfo is None and current.tzinfo is not None:
        last = last.replace(tzinfo=current.tzinfo)
    return current - last >= timedelta(days=interval_days)


def record_deep_scan_done(source_id: str, *, now: datetime | None = None) -> None:
    state = _load_state()
    state[source_id] = {LAST_SCAN_KEY: (now or _now()).astimezone().isoformat(timespec="seconds")}
    dump_json_utf8(DEEP_SCAN_STATE_PATH, state)

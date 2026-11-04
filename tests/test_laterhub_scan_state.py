"""深度扫描到期状态：读不懂一律到期，写入只动当前源键。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from apps.laterhub import scan_state

TZ = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(scan_state, "DEEP_SCAN_STATE_PATH", tmp_path / "deep_scan.json")


def _now() -> datetime:
    return datetime(2026, 11, 4, 7, 10, tzinfo=TZ)


def _write(payload) -> None:
    scan_state.DEEP_SCAN_STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_missing_state_is_due():
    assert scan_state.deep_scan_due("douyin_favorite", 30, now=_now()) is True


def test_corrupt_state_is_due():
    scan_state.DEEP_SCAN_STATE_PATH.write_text("{not json", encoding="utf-8")
    assert scan_state.deep_scan_due("douyin_favorite", 30, now=_now()) is True


def test_unparseable_timestamp_is_due():
    _write({"douyin_favorite": {"last_deep_scan_at": "昨天"}})
    assert scan_state.deep_scan_due("douyin_favorite", 30, now=_now()) is True


def test_within_interval_is_not_due():
    _write({"douyin_favorite": {"last_deep_scan_at": "2026-10-20T07:10:00+08:00"}})
    assert scan_state.deep_scan_due("douyin_favorite", 30, now=_now()) is False


def test_older_than_interval_is_due():
    _write({"douyin_favorite": {"last_deep_scan_at": "2026-09-30T07:10:00+08:00"}})
    assert scan_state.deep_scan_due("douyin_favorite", 30, now=_now()) is True


def test_record_writes_timestamp_and_keeps_other_sources():
    _write({"xiaoheihe_favor": {"last_deep_scan_at": "2026-10-01T07:00:00+08:00"}})

    scan_state.record_deep_scan_done("douyin_favorite", now=_now())

    payload = json.loads(scan_state.DEEP_SCAN_STATE_PATH.read_text(encoding="utf-8"))
    assert payload["douyin_favorite"] == {"last_deep_scan_at": "2026-11-04T07:10:00+08:00"}
    assert payload["xiaoheihe_favor"] == {"last_deep_scan_at": "2026-10-01T07:00:00+08:00"}
    # 刚记录完不再到期
    assert scan_state.deep_scan_due("douyin_favorite", 30, now=_now()) is False

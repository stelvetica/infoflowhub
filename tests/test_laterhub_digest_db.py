"""laterhub「视频简报」状态机：links 表 digest_* 列、领取/回写/回收方法。

不触网络、不依赖 videocaptioner：只验证 db.py 的迁移与状态流转。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from apps.laterhub.db import DBManager, LinkRecord

BILI = "https://www.bilibili.com/video/BV1xx411c7mD"
BILI2 = "https://www.bilibili.com/video/BV1yy411c7mE"
DOUYIN = "https://www.douyin.com/video/7123456789"
ARTICLE = "https://mp.weixin.qq.com/s/abcdefg"

DIGEST_COLUMNS = {"digest_status", "digest_path", "digest_at", "digest_error", "digest_attempts"}


def _add(db: DBManager, url: str, *, source: str = "bilibili_watchlater", title: str = "视频") -> int:
    return db.upsert_link(LinkRecord(url=url, title=title, source=source))


def _columns(db_path) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("PRAGMA table_info(links)").fetchall()
    finally:
        conn.close()
    return {row[1]: row for row in rows}


def _raw_update(db_path, link_id: int, **values) -> None:
    assignments = ", ".join(f"{k} = ?" for k in values)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"UPDATE links SET {assignments} WHERE id = ?", (*values.values(), link_id))
        conn.commit()
    finally:
        conn.close()


def test_legacy_db_migration_adds_digest_columns(tmp_path):
    """旧库（无 digest 列）初始化后列齐全。"""
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            tags TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            tag_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            pushed_at TEXT,
            tag_error_message TEXT,
            error_message TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    DBManager(db_path)

    cols = _columns(db_path)
    assert DIGEST_COLUMNS <= set(cols)
    assert cols["digest_status"][4] == "'pending'" and cols["digest_status"][3] == 1
    assert cols["digest_attempts"][4] == "0" and cols["digest_attempts"][3] == 1
    assert cols["digest_path"][3] == 0 and cols["digest_at"][3] == 0 and cols["digest_error"][3] == 0


def test_migration_is_idempotent_and_keeps_data(tmp_path):
    db_path = tmp_path / "repeat.sqlite3"
    db = DBManager(db_path)
    link_id = _add(db, BILI)

    DBManager(db_path)  # 重复初始化
    DBManager(db_path)

    row = DBManager(db_path).get_digest_row(link_id)
    assert row is not None
    assert row["url"] == BILI
    assert row["digest_status"] == "pending"
    assert row["digest_attempts"] == 0


def test_candidates_defaults_to_pending_videos(tmp_path):
    db = DBManager(tmp_path / "c.sqlite3")
    pending_bili = _add(db, BILI)
    pending_douyin = _add(db, DOUYIN, source="douyin_favorites")
    failed_bili = _add(db, BILI2)
    unsupported = _add(db, "https://www.bilibili.com/video/BV1zz411c7mF")
    _add(db, ARTICLE)  # 非视频，不入选
    db.mark_digest_failed(failed_bili, error_message="boom")
    db.mark_digest_unsupported(unsupported, error_message="不支持")

    rows = db.list_digest_candidates(limit=10)

    assert {row["id"] for row in rows} == {pending_bili, pending_douyin}


def test_candidates_retry_failed_includes_failed_not_unsupported(tmp_path):
    db = DBManager(tmp_path / "c2.sqlite3")
    pending = _add(db, BILI)
    failed = _add(db, BILI2)
    unsupported = _add(db, "https://www.bilibili.com/video/BV1zz411c7mF")
    db.mark_digest_failed(failed, error_message="boom")
    db.mark_digest_unsupported(unsupported, error_message="不支持")

    rows = db.list_digest_candidates(limit=10, retry_failed=True)

    ids = {row["id"] for row in rows}
    assert ids == {pending, failed}
    assert unsupported not in ids


def test_candidates_source_filter_and_limit_and_order(tmp_path):
    db = DBManager(tmp_path / "c3.sqlite3")
    first = _add(db, BILI, source="bilibili_watchlater")
    second = _add(db, BILI2, source="bilibili_watchlater")
    _add(db, DOUYIN, source="douyin_favorites")

    rows = db.list_digest_candidates(limit=10, source="bilibili_watchlater")
    assert {row["id"] for row in rows} == {first, second}

    limited = db.list_digest_candidates(limit=1, source="bilibili_watchlater")
    assert len(limited) == 1
    assert limited[0]["id"] == second  # ORDER BY id DESC

    assert db.list_digest_candidates(limit=10, source="不存在") == []


def test_claim_pending_then_running_is_rejected(tmp_path):
    db = DBManager(tmp_path / "cl.sqlite3")
    link_id = _add(db, BILI)

    assert db.claim_digest(link_id) is True
    row = db.get_digest_row(link_id)
    assert row["digest_status"] == "running"
    assert row["digest_attempts"] == 1
    assert row["digest_error"] is None

    assert db.claim_digest(link_id) is False  # 已 running
    assert db.get_digest_row(link_id)["digest_attempts"] == 1


def test_claim_retry_failed_gate(tmp_path):
    db = DBManager(tmp_path / "cl2.sqlite3")
    failed = _add(db, BILI)
    db.mark_digest_failed(failed, error_message="boom")

    assert db.claim_digest(failed) is False  # 默认不含 failed
    assert db.claim_digest(failed, retry_failed=True) is True
    assert db.get_digest_row(failed)["digest_attempts"] == 1

    unsupported = _add(db, BILI2)
    db.mark_digest_unsupported(unsupported, error_message="不支持")
    assert db.claim_digest(unsupported, retry_failed=True) is False
    assert db.claim_digest(unsupported) is False


def test_claim_is_atomic_across_instances(tmp_path):
    db_path = tmp_path / "race.sqlite3"
    link_id = _add(DBManager(db_path), BILI)

    a = DBManager(db_path)
    b = DBManager(db_path)
    results = [a.claim_digest(link_id), b.claim_digest(link_id)]

    assert results.count(True) == 1
    assert results.count(False) == 1


def test_mark_statuses_write_expected_fields(tmp_path):
    db = DBManager(tmp_path / "m.sqlite3")

    ok = _add(db, BILI)
    db.mark_digest_succeeded(ok, digest_path="bili_BV1xx.md", completed_at="2026-09-29T10:00:00+00:00")
    row = db.get_digest_row(ok)
    assert row["digest_status"] == "succeeded"
    assert row["digest_path"] == "bili_BV1xx.md"
    assert row["digest_at"] == "2026-09-29T10:00:00+00:00"
    assert row["digest_error"] is None

    bad = _add(db, BILI2)
    db.mark_digest_failed(bad, error_message="x" * 600)
    row = db.get_digest_row(bad)
    assert row["digest_status"] == "failed"
    assert row["digest_error"] == "x" * 500  # 截断到 500

    unsupported = _add(db, DOUYIN, source="douyin_favorites")
    db.mark_digest_unsupported(unsupported, error_message="抖音暂未支持")
    row = db.get_digest_row(unsupported)
    assert row["digest_status"] == "unsupported"
    assert row["digest_error"] == "抖音暂未支持"


def test_recover_stale_digest_runs(tmp_path):
    db_path = tmp_path / "stale.sqlite3"
    db = DBManager(db_path)
    stale = _add(db, BILI)
    fresh = _add(db, BILI2)
    assert db.claim_digest(stale) is True
    assert db.claim_digest(fresh) is True

    old = (datetime.now(timezone.utc) - timedelta(seconds=10_000)).isoformat(timespec="seconds")
    _raw_update(db_path, stale, updated_at=old)

    assert db.recover_stale_digest_runs() == 1
    stale_row = db.get_digest_row(stale)
    assert stale_row["digest_status"] == "pending"
    assert stale_row["digest_error"] == "上次任务中断，已重新排队"
    assert db.get_digest_row(fresh)["digest_status"] == "running"


def test_get_digest_stats_counts_videos_only(tmp_path):
    db = DBManager(tmp_path / "s.sqlite3")
    _add(db, BILI)  # pending
    running = _add(db, BILI2)
    succeeded = _add(db, "https://www.bilibili.com/video/BV1aa411c7mG")
    failed = _add(db, "https://www.bilibili.com/video/BV1bb411c7mH")
    unsupported = _add(db, DOUYIN, source="douyin_favorites")
    _add(db, ARTICLE)  # 非视频，不计数

    db.claim_digest(running)
    db.mark_digest_succeeded(succeeded, digest_path="a.md", completed_at="2026-09-29T10:00:00+00:00")
    db.mark_digest_failed(failed, error_message="boom")
    db.mark_digest_unsupported(unsupported, error_message="不支持")

    assert db.get_digest_stats() == {
        "pending": 1,
        "running": 1,
        "succeeded": 1,
        "failed": 1,
        "unsupported": 1,
    }


def test_get_digest_row_missing_returns_none(tmp_path):
    assert DBManager(tmp_path / "n.sqlite3").get_digest_row(9999) is None

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from apps.subscriptions import rss_db
from apps.subscriptions.models import FeedEntry


def _entries(count: int, summary_size: int = 0, start_hour: int = 0) -> list[FeedEntry]:
    base = datetime(2026, 1, 1, 0, 0)
    return [
        FeedEntry(
            source_id="demo",
            source_name="Demo",
            title=f"Item {idx:03d}",
            link=f"https://example.com/{idx}",
            published=(base + timedelta(hours=start_hour + idx)).strftime("%Y/%m/%d %H:%M"),
            summary="x" * summary_size,
        )
        for idx in range(count)
    ]


def _prepare(monkeypatch, tmp_path):
    db_path = tmp_path / "subscriptions.sqlite3"
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", db_path)
    return db_path


def _titles(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT title FROM rss_entries ORDER BY published_at, id")]
    finally:
        conn.close()


def test_under_limit_keeps_all_history(monkeypatch, tmp_path):
    db_path = _prepare(monkeypatch, tmp_path)
    rss_db.save_entries(_entries(20, summary_size=200))

    conn = rss_db.get_connection()
    try:
        deleted = rss_db.prune_entries_over_limit(conn, limit_bytes=10 * 1024 * 1024)
    finally:
        conn.close()

    assert deleted == 0
    assert len(_titles(db_path)) == 20


def test_over_limit_deletes_oldest_until_under_limit(monkeypatch, tmp_path):
    db_path = _prepare(monkeypatch, tmp_path)
    rss_db.save_entries(_entries(50, summary_size=2000))

    limit = db_path.stat().st_size // 2
    conn = rss_db.get_connection()
    try:
        deleted = rss_db.prune_entries_over_limit(conn, limit_bytes=limit)
    finally:
        conn.close()

    titles = _titles(db_path)
    assert deleted > 0
    assert db_path.stat().st_size <= limit
    # 删的是最旧的一批，剩下的必须是最新的连续区间
    assert titles == [f"Item {idx:03d}" for idx in range(50 - len(titles), 50)]


def test_save_entries_prunes_automatically_over_limit(monkeypatch, tmp_path):
    db_path = _prepare(monkeypatch, tmp_path)
    rss_db.save_entries(_entries(40, summary_size=2000))

    limit = 60_000
    monkeypatch.setattr(rss_db, "MAX_DB_BYTES", limit)
    rss_db.save_entries(_entries(41, summary_size=2000))

    titles = _titles(db_path)
    assert db_path.stat().st_size <= limit
    assert titles[-1] == "Item 040"
    assert len(titles) < 41
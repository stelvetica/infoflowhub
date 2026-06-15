from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from apps.subscriptions import rss_db
from apps.subscriptions.models import FeedEntry
from connectors.rss.fetch import trim_entries
from connectors._shared.common import with_query_params
from connectors.youtube.feed import normalize_youtube_published


def test_youtube_page_url_can_pin_english_locale():
    url = with_query_params("https://www.youtube.com/@demo/videos?view=0", {"hl": "en", "gl": "US"})

    assert url == "https://www.youtube.com/@demo/videos?view=0&hl=en&gl=US"


def test_youtube_relative_published_text_normalizes():
    assert normalize_youtube_published("2 days ago")


def test_save_entries_dedups_by_source_title(monkeypatch, tmp_path):
    db_path = tmp_path / "subscriptions.sqlite3"
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", db_path)

    entries = [
        FeedEntry(
            source_id="youtube-demo",
            source_name="YouTube Demo",
            title="Same Video Title",
            link="https://www.youtube.com/watch?v=old",
            published="2026/05/24 08:00",
            summary="old",
        ),
        FeedEntry(
            source_id="youtube-demo",
            source_name="YouTube Demo",
            title=" same video title ",
            link="https://www.youtube.com/watch?v=new",
            published="2026/05/25 08:00",
            summary="new",
        ),
    ]

    rss_db.save_entries(entries)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT title, link FROM rss_entries").fetchall()
    finally:
        conn.close()

    assert rows == [("Same Video Title", "https://www.youtube.com/watch?v=old")]


def test_save_entries_uses_partial_unique_link_index(monkeypatch, tmp_path):
    db_path = tmp_path / "subscriptions.sqlite3"
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", db_path)

    rss_db.save_entries(
        [
            FeedEntry(
                source_id="source-a",
                source_name="Source A",
                title="Title A",
                link="https://example.com/shared",
                published="2026/05/24 08:00",
                summary="old",
            ),
            FeedEntry(
                source_id="source-b",
                source_name="Source B",
                title="Title B",
                link="https://example.com/shared",
                published="2026/05/25 08:00",
                summary="new",
            ),
        ]
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT source_id, source_name, title, link, published FROM rss_entries ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert rows == [
        ("source-b", "Source B", "Title B", "https://example.com/shared", "2026/05/25 08:00"),
    ]


def test_trim_entries_keeps_at_least_ten_when_three_days_not_reached():
    now = datetime.now()
    entries = [
        FeedEntry(
            source_id="demo",
            source_name="Demo",
            title=f"Item {idx}",
            link=f"https://example.com/{idx}",
            published=(now - timedelta(days=0 if idx < 8 else 4)).strftime("%Y/%m/%d %H:%M"),
            summary="x",
        )
        for idx in range(12)
    ]

    trimmed = trim_entries(entries)

    assert len(trimmed) == 10

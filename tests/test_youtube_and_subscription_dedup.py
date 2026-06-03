from __future__ import annotations

import sqlite3

from apps.subscriptions import rss_db
from apps.subscriptions.models import FeedEntry
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

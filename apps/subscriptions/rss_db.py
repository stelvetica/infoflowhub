from __future__ import annotations

import sqlite3
import re
from pathlib import Path
from typing import Iterable

from apps.subscriptions.models import FeedEntry
from connectors.web.common import parse_published_datetime


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "subscriptions.sqlite3"


SCHEMA = """
CREATE TABLE IF NOT EXISTS rss_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  title TEXT NOT NULL,
  link TEXT NOT NULL,
  published TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_id, link)
);

CREATE TABLE IF NOT EXISTS rss_source_state (
  source_id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(rss_entries)").fetchall()}
    if "published_at" not in columns:
        conn.execute("ALTER TABLE rss_entries ADD COLUMN published_at TEXT NOT NULL DEFAULT ''")
        conn.commit()
    conn.execute(
        """
        UPDATE rss_entries
        SET published_at = ?
        WHERE published_at = ? AND 1 = 0
        """,
        ("", ""),
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_rss_entries_published_at
        ON rss_entries(published_at DESC, id DESC)
        """
    )
    conn.commit()


def normalize_published_text(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parsed = parse_published_datetime(text)
    if not parsed:
        return text.replace("-", "/")[:16]
    return parsed.strftime("%Y/%m/%d %H:%M")


def list_source_enabled_state() -> dict[str, bool]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT source_id, enabled
            FROM rss_source_state
            """
        ).fetchall()
    finally:
        conn.close()
    return {str(row["source_id"]): bool(row["enabled"]) for row in rows}


def sanitize_db_text(value: str) -> str:
    return re.sub(r"[\ud800-\udfff]", "", value or "")


def set_source_enabled(source_id: str, enabled: bool) -> None:
    source_id = sanitize_db_text(source_id).strip()
    if not source_id:
        return
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO rss_source_state (source_id, enabled, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_id) DO UPDATE SET
              enabled = excluded.enabled,
              updated_at = CURRENT_TIMESTAMP
            """,
            (source_id, 1 if enabled else 0),
        )
        conn.commit()
    finally:
        conn.close()


def save_entries(entries: Iterable[FeedEntry]) -> int:
    conn = get_connection()
    inserted = 0
    try:
        for item in entries:
            normalized_published = normalize_published_text(item.published)
            cursor = conn.execute(
                """
                INSERT INTO rss_entries
                (source_id, source_name, title, link, published, published_at, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, link) DO UPDATE SET
                  source_name=excluded.source_name,
                  title=CASE WHEN excluded.title != '' THEN excluded.title ELSE rss_entries.title END,
                  published=CASE WHEN excluded.published != '' THEN excluded.published ELSE rss_entries.published END,
                  published_at=CASE WHEN excluded.published_at != '' THEN excluded.published_at ELSE rss_entries.published_at END,
                  summary=CASE WHEN excluded.summary != '' THEN excluded.summary ELSE rss_entries.summary END
                """,
                (
                    item.source_id,
                    item.source_name,
                    item.title,
                    item.link,
                    normalized_published,
                    normalized_published,
                    item.summary,
                ),
            )
            inserted += int(cursor.rowcount > 0)
        conn.commit()
    finally:
        conn.close()
    return inserted


def list_entries(limit: int = 200, source_id: str = "") -> list[dict]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        if source_id:
            rows = conn.execute(
                """
                SELECT source_id, source_name, title, link, published, published_at, summary, created_at
                FROM rss_entries
                WHERE source_id = ?
                ORDER BY COALESCE(NULLIF(published_at, ''), NULLIF(published, ''), created_at) DESC, id DESC
                LIMIT ?
                """,
                (source_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT source_id, source_name, title, link, published, published_at, summary, created_at
                FROM rss_entries
                ORDER BY COALESCE(NULLIF(published_at, ''), NULLIF(published, ''), created_at) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def delete_source_state(source_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            DELETE FROM rss_source_state
            WHERE source_id = ?
            """,
            (source_id,),
        )
        conn.commit()
    finally:
        conn.close()


def list_source_stats() -> list[dict]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT source_id, source_name, COUNT(*) AS entry_count,
                   MAX(COALESCE(NULLIF(published_at, ''), NULLIF(published, ''), created_at)) AS last_seen
            FROM rss_entries
            GROUP BY source_id, source_name
            ORDER BY source_name COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def delete_entries_by_source(source_id: str) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            DELETE FROM rss_entries
            WHERE source_id = ?
            """,
            (source_id,),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()

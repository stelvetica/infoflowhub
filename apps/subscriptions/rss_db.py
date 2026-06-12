from __future__ import annotations

import sqlite3
import re
from pathlib import Path
from typing import Iterable

from apps.subscriptions.models import FeedEntry
from apps.subscriptions.source_ids import canonicalize_source_id, legacy_source_ids
from connectors._shared.common import parse_published_datetime
from infra.text_normalizer import normalize_utf8_text


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
  markdown_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rss_entry_reads (
  read_key TEXT PRIMARY KEY,
  read_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    ensure_schema(conn)
    return conn


def _table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return str(row[0] or "") if row else ""


def _rebuild_rss_entries_without_legacy_unique(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rss_entries__new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id TEXT NOT NULL,
          source_name TEXT NOT NULL,
          title TEXT NOT NULL,
          link TEXT NOT NULL,
          published TEXT NOT NULL DEFAULT '',
          published_at TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO rss_entries__new (id, source_id, source_name, title, link, published, published_at, summary, created_at)
        SELECT id, source_id, source_name, title, link, published, published_at, summary, created_at
        FROM rss_entries;
        DROP TABLE rss_entries;
        ALTER TABLE rss_entries__new RENAME TO rss_entries;
        """
    )
    conn.commit()


def ensure_schema(conn: sqlite3.Connection) -> None:
    table_sql = _table_sql(conn, "rss_entries")
    if "UNIQUE(source_id, link)" in table_sql:
        _rebuild_rss_entries_without_legacy_unique(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(rss_entries)").fetchall()}
    if "published_at" not in columns:
        conn.execute("ALTER TABLE rss_entries ADD COLUMN published_at TEXT NOT NULL DEFAULT ''")
        conn.commit()
        columns.add("published_at")
    if "markdown_path" not in columns:
        conn.execute("ALTER TABLE rss_entries ADD COLUMN markdown_path TEXT NOT NULL DEFAULT ''")
        conn.commit()
        columns.add("markdown_path")
    required_columns = {"source_id", "source_name", "title", "link", "published", "published_at", "summary", "markdown_path", "created_at"}
    if not required_columns.issubset(columns):
        return
    conn.execute(
        """
        DELETE FROM rss_entries
        WHERE id IN (
            SELECT older.id
            FROM rss_entries AS older
            JOIN rss_entries AS newer
              ON older.link = newer.link
             AND older.source_id != 'alphapai'
             AND newer.source_id != 'alphapai'
             AND (
                  COALESCE(NULLIF(older.published_at, ''), NULLIF(older.published, ''), older.created_at)
                    < COALESCE(NULLIF(newer.published_at, ''), NULLIF(newer.published, ''), newer.created_at)
                  OR (
                    COALESCE(NULLIF(older.published_at, ''), NULLIF(older.published, ''), older.created_at)
                      = COALESCE(NULLIF(newer.published_at, ''), NULLIF(newer.published, ''), newer.created_at)
                    AND older.id < newer.id
                  )
             )
        )
        """
    )
    conn.execute(
        """
        DELETE FROM rss_entries
        WHERE id IN (
            SELECT older.id
            FROM rss_entries AS older
            JOIN rss_entries AS newer
              ON older.source_id = newer.source_id
             AND LOWER(TRIM(older.title)) = LOWER(TRIM(newer.title))
             AND (
                  COALESCE(NULLIF(older.published_at, ''), NULLIF(older.published, ''), older.created_at)
                    < COALESCE(NULLIF(newer.published_at, ''), NULLIF(newer.published, ''), newer.created_at)
                  OR (
                    COALESCE(NULLIF(older.published_at, ''), NULLIF(older.published, ''), older.created_at)
                      = COALESCE(NULLIF(newer.published_at, ''), NULLIF(newer.published, ''), newer.created_at)
                    AND older.id < newer.id
                  )
             )
        )
        """
    )
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
    conn.execute("DROP INDEX IF EXISTS idx_rss_entries_source_link_unique")
    conn.execute("DROP INDEX IF EXISTS idx_rss_entries_link_unique")
    conn.execute("DROP INDEX IF EXISTS idx_rss_entries_link_unique_non_alphapai")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_entries_link_unique_non_alphapai
        ON rss_entries(link)
        WHERE source_id != 'alphapai'
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_rss_entries_source_title
        ON rss_entries(source_id, title)
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


def sanitize_db_text(value: str) -> str:
    return re.sub(r"[\ud800-\udfff]", "", normalize_utf8_text(value or ""))


def normalize_title_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def derive_alphapai_title_from_summary(summary: str, fallback_title: str) -> str:
    lines = [sanitize_db_text(line).strip() for line in str(summary or "").splitlines()]
    for line in lines[:8]:
        if not line:
            continue
        if line.startswith("分享") or line.startswith("播放") or line.startswith("时长"):
            continue
        if "免责声明" in line or "投资建议" in line:
            continue
        if len(line) < 6:
            continue
        return line[:180]
    return sanitize_db_text(fallback_title)


def rename_source(source_id: str, source_name: str) -> int:
    source_id = canonicalize_source_id(sanitize_db_text(source_id).strip())
    source_name = sanitize_db_text(source_name).strip()
    if not source_id or not source_name:
        return 0
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE rss_entries
            SET source_name = ?
            WHERE source_id = ?
            """,
            (source_name, source_id),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def save_entries(entries: Iterable[FeedEntry]) -> int:
    conn = get_connection()
    inserted = 0
    try:
        for item in entries:
            source_id = canonicalize_source_id(item.source_id)
            title = sanitize_db_text(item.title)
            title_key = normalize_title_key(title)
            link = item.link.strip()
            normalized_published = normalize_published_text(item.published)

            existing = conn.execute(
                "SELECT id, link FROM rss_entries WHERE source_id = ? AND LOWER(TRIM(title)) = ?",
                (source_id, title_key),
            ).fetchone()
            if existing:
                existing_id, existing_link = existing
                if link and not existing_link:
                    conn.execute(
                        "UPDATE rss_entries SET link = ? WHERE id = ?",
                        (link, existing_id),
                    )
                continue

            if source_id == "alphapai":
                cursor = conn.execute(
                    """
                    INSERT INTO rss_entries
                    (source_id, source_name, title, link, published, published_at, summary, markdown_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        sanitize_db_text(item.source_name),
                        title,
                        link,
                        normalized_published,
                        normalized_published,
                        sanitize_db_text(item.summary),
                        sanitize_db_text(getattr(item, "markdown_path", "")),
                    ),
                )
                inserted += int(cursor.rowcount > 0)
                continue

            cursor = conn.execute(
                """
                INSERT INTO rss_entries
                (source_id, source_name, title, link, published, published_at, summary, markdown_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(link) DO UPDATE SET
                  source_id=excluded.source_id,
                  source_name=excluded.source_name,
                  title=CASE WHEN excluded.title != '' THEN excluded.title ELSE rss_entries.title END,
                  published=CASE WHEN excluded.published != '' THEN excluded.published ELSE rss_entries.published END,
                  published_at=CASE WHEN excluded.published_at != '' THEN excluded.published_at ELSE rss_entries.published_at END,
                  summary=CASE WHEN excluded.summary != '' THEN excluded.summary ELSE rss_entries.summary END,
                  markdown_path=CASE WHEN excluded.markdown_path != '' THEN excluded.markdown_path ELSE rss_entries.markdown_path END
                """,
                (
                    source_id,
                    sanitize_db_text(item.source_name),
                    title,
                    link,
                    normalized_published,
                    normalized_published,
                    sanitize_db_text(item.summary),
                    sanitize_db_text(getattr(item, "markdown_path", "")),
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
            source_id = canonicalize_source_id(source_id)
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
    return [
        {
            "source_id": canonicalize_source_id(str(row["source_id"] or "")),
            "source_name": normalize_utf8_text(row["source_name"]),
            "title": normalize_utf8_text(row["title"]),
            "link": str(row["link"] or "").strip(),
            "published": normalize_utf8_text(row["published"]),
            "published_at": normalize_utf8_text(row["published_at"]),
            "summary": normalize_utf8_text(row["summary"]),
            "markdown_path": str(row["markdown_path"] or "").strip() if "markdown_path" in row.keys() else "",
            "created_at": normalize_utf8_text(row["created_at"]),
        }
        for row in rows
    ]


def delete_source_state(source_id: str) -> None:
    source_id = canonicalize_source_id(sanitize_db_text(source_id).strip())
    if not source_id:
        return
    target_ids = [source_id, *legacy_source_ids(source_id)]
    placeholders = ", ".join("?" for _ in target_ids)
    conn = get_connection()
    try:
        conn.execute(
            f"""
            DELETE FROM rss_source_state
            WHERE source_id IN ({placeholders})
            """,
            target_ids,
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
    merged: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        source_id = canonicalize_source_id(item["source_id"])
        current = merged.get(source_id)
        if not current:
            merged[source_id] = {
                "source_id": source_id,
                "source_name": normalize_utf8_text(item["source_name"]),
                "entry_count": int(item["entry_count"] or 0),
                "last_seen": item["last_seen"],
            }
            continue
        current["entry_count"] += int(item["entry_count"] or 0)
        if str(item["last_seen"] or "") > str(current.get("last_seen") or ""):
            current["last_seen"] = item["last_seen"]
            current["source_name"] = normalize_utf8_text(item["source_name"])
    return sorted(merged.values(), key=lambda item: str(item.get("source_name") or "").lower())


def normalize_existing_entries() -> int:
    conn = get_connection()
    updated = 0
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, source_id, source_name, title, published, published_at, summary, created_at
            FROM rss_entries
            """
        ).fetchall()
        for row in rows:
            normalized = {
                "source_id": canonicalize_source_id(str(row["source_id"] or "")),
                "source_name": sanitize_db_text(str(row["source_name"] or "")),
                "title": sanitize_db_text(str(row["title"] or "")),
                "published": normalize_utf8_text(str(row["published"] or "")),
                "published_at": normalize_utf8_text(str(row["published_at"] or "")),
                "summary": sanitize_db_text(str(row["summary"] or "")),
                "created_at": normalize_utf8_text(str(row["created_at"] or "")),
            }
            current = {
                "source_id": str(row["source_id"] or ""),
                "source_name": str(row["source_name"] or ""),
                "title": str(row["title"] or ""),
                "published": str(row["published"] or ""),
                "published_at": str(row["published_at"] or ""),
                "summary": str(row["summary"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
            if current == normalized:
                continue
            conn.execute(
                """
                UPDATE rss_entries
                SET source_id = ?, source_name = ?, title = ?, published = ?, published_at = ?, summary = ?, created_at = ?
                WHERE id = ?
                """,
                (
                    normalized["source_id"],
                    normalized["source_name"],
                    normalized["title"],
                    normalized["published"],
                    normalized["published_at"],
                    normalized["summary"],
                    normalized["created_at"],
                    int(row["id"]),
                ),
            )
            updated += 1
        if updated:
            conn.commit()
    finally:
        conn.close()
    return updated


def delete_entries_by_source(source_id: str) -> int:
    source_id = canonicalize_source_id(sanitize_db_text(source_id).strip())
    if not source_id:
        return 0
    target_ids = [source_id, *legacy_source_ids(source_id)]
    placeholders = ", ".join("?" for _ in target_ids)
    conn = get_connection()
    try:
        cursor = conn.execute(
            f"""
            DELETE FROM rss_entries
            WHERE source_id IN ({placeholders})
            """,
            target_ids,
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def migrate_legacy_source_ids() -> int:
    from apps.subscriptions.source_ids import SOURCE_ID_ALIASES

    if not SOURCE_ID_ALIASES:
        return 0
    conn = get_connection()
    migrated = 0
    try:
        for legacy_source_id, canonical_source_id in SOURCE_ID_ALIASES.items():
            cursor = conn.execute(
                """
                UPDATE rss_entries
                SET source_id = ?
                WHERE source_id = ?
                """,
                (canonical_source_id, legacy_source_id),
            )
            migrated += int(cursor.rowcount or 0)
            conn.execute(
                """
                UPDATE rss_source_state
                SET source_id = ?
                WHERE source_id = ?
                """,
                (canonical_source_id, legacy_source_id),
            )
            conn.execute(
                """
                DELETE FROM rss_source_state
                WHERE rowid NOT IN (
                    SELECT MIN(rowid)
                    FROM rss_source_state
                    GROUP BY source_id
                )
                """
            )
        conn.commit()
        return migrated
    finally:
        conn.close()


def normalize_alphapai_entries() -> int:
    conn = get_connection()
    updated = 0
    try:
        rows = conn.execute(
            """
            SELECT id, title, summary, link
            FROM rss_entries
            WHERE source_id = 'alphapai'
            """
        ).fetchall()
        for row in rows:
            row_id, old_title, summary, old_link = row
            new_title = derive_alphapai_title_from_summary(summary, old_title)
            new_link = "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"
            if str(old_title or "") == new_title and str(old_link or "") == new_link:
                continue
            conn.execute(
                """
                UPDATE rss_entries
                SET title = ?, link = ?
                WHERE id = ?
                """,
                (new_title, new_link, int(row_id)),
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def list_read_entry_keys() -> set[str]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT read_key FROM rss_entry_reads").fetchall()
    finally:
        conn.close()
    return {str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()}


def mark_entry_read(read_key: str) -> bool:
    normalized = sanitize_db_text(read_key).strip()
    if not normalized:
        return False
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO rss_entry_reads (read_key, read_at)
            VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(read_key) DO UPDATE SET read_at = excluded.read_at
            """,
            (normalized,),
        )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()

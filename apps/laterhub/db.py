from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


UTC = timezone.utc


@dataclass(slots=True)
class LinkRecord:
    url: str
    title: str
    source: str
    tags: str | None = None
    status: str = "pending"
    tag_status: str = "pending"
    tag_error_message: str | None = None
    error_message: str | None = None


class DBManager:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS links (
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
                    error_message TEXT,
                    feishu_record_id TEXT
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(links)").fetchall()}
            if "feishu_record_id" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN feishu_record_id TEXT")
            if "tag_status" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN tag_status TEXT NOT NULL DEFAULT 'pending'")
            if "tag_error_message" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN tag_error_message TEXT")
            if "is_finished" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN is_finished INTEGER NOT NULL DEFAULT 0")
            if "finished_at" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN finished_at TEXT")
            if "is_opened" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN is_opened INTEGER NOT NULL DEFAULT 0")
            if "opened_at" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN opened_at TEXT")
            if "feishu_last_synced_at" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN feishu_last_synced_at TEXT")
            conn.execute(
                """
                UPDATE links
                SET tag_status = 'done',
                    tag_error_message = NULL
                WHERE COALESCE(TRIM(tags), '') <> ''
                  AND tag_status = 'pending'
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_links_status ON links(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_links_tag_status ON links(tag_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_links_source ON links(source)")

    def upsert_link(self, record: LinkRecord) -> int:
        now = self._now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM links WHERE url = ?",
                (record.url,),
            ).fetchone()

            if row:
                conn.execute(
                    """
                    UPDATE links
                    SET title = ?,
                        source = ?,
                        tags = COALESCE(?, tags),
                        tag_status = CASE
                            WHEN COALESCE(?, '') <> '' THEN 'done'
                            ELSE 'pending'
                        END,
                        tag_error_message = CASE
                            WHEN COALESCE(?, '') <> '' THEN NULL
                            ELSE tag_error_message
                        END,
                        is_finished = 0,
                        finished_at = NULL,
                        updated_at = ?,
                        error_message = ?
                    WHERE url = ?
                    """,
                    (
                        record.title,
                        record.source,
                        record.tags,
                        record.tags,
                        record.tags,
                        now,
                        record.error_message,
                        record.url,
                    ),
                )
                return int(row["id"])

            cursor = conn.execute(
                """
                INSERT INTO links (
                    url, title, source, tags, status, tag_status, created_at, updated_at, pushed_at, tag_error_message, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.url,
                    record.title,
                    record.source,
                    record.tags,
                    record.status,
                    "done" if record.tags else record.tag_status,
                    now,
                    now,
                    None,
                    record.tag_error_message,
                    record.error_message,
                ),
            )
            return int(cursor.lastrowid)

    def list_by_status(self, *statuses: str):
        if not statuses:
            statuses = ("pending",)
        placeholders = ", ".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM links WHERE status IN ({placeholders}) ORDER BY id ASC",
                statuses,
            ).fetchall()
        return list(rows)

    def list_pending_tag_rows(self):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM links
                WHERE status = 'pending'
                  AND tag_status = 'pending'
                ORDER BY id ASC
                """
            ).fetchall()
        return list(rows)

    def list_untagged_rows(self):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM links
                WHERE COALESCE(TRIM(tags), '') = ''
                  AND tag_status <> 'done'
                ORDER BY id ASC
                """
            ).fetchall()
        return list(rows)

    def update_tags(self, url: str, tags: str, *, tag_status: str = "done", tag_error_message: str | None = None) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET tags = ?,
                    tag_status = ?,
                    tag_error_message = ?,
                    updated_at = ?
                WHERE url = ?
                """,
                (tags, tag_status, tag_error_message, now, url),
            )

    def mark_tag_skipped(self, url: str, tags: str, reason: str) -> None:
        self.update_tags(url, tags, tag_status="skipped", tag_error_message=reason)

    def mark_tag_pending_retry(self, url: str, reason: str) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET tags = NULL,
                    tag_status = 'pending',
                    tag_error_message = ?,
                    updated_at = ?
                WHERE url = ?
                """,
                (reason, now, url),
            )

    def mark_pending_retry(self, url: str, error_message: str) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET status = 'pending',
                    updated_at = ?,
                    error_message = ?
                WHERE url = ?
                """,
                (now, error_message, url),
            )

    def mark_pushed(self, url: str, feishu_record_id: str | None = None) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET status = 'pushed',
                    pushed_at = ?,
                    updated_at = ?,
                    error_message = NULL,
                    feishu_record_id = COALESCE(?, feishu_record_id)
                WHERE url = ?
                """,
                (now, now, feishu_record_id, url),
            )

    def mark_failed(self, url: str, error_message: str) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET status = 'failed',
                    updated_at = ?,
                    error_message = ?
                WHERE url = ?
                """,
                (now, error_message, url),
            )

    def reset_failed_to_pending(self) -> int:
        now = self._now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE links
                SET status = 'pending',
                    updated_at = ?,
                    error_message = NULL
                WHERE status = 'failed'
                """,
                (now,),
            )
            return int(cursor.rowcount)

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _now_iso() -> str:
        return DBManager.now_iso()

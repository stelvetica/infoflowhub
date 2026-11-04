from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse


UTC = timezone.utc

# 视频简报候选：只看视频条目（B站/抖音）。
VIDEO_URL_CONDITION = "(url LIKE '%bilibili.com/video/%' OR url LIKE '%douyin.com/video/%')"


def is_digest_video_url(url: str | None) -> bool:
    """是否 B站/抖音的视频页（UI 是否展示「解析」的唯一判据）。

    按真实域名 + 路径判定，不能只看子串：否则「普通文章里带 B站链接」的转发 URL
    也会被误判成视频。VIDEO_URL_CONDITION 是 SQL 侧粗筛（候选集），此处是精确判据。
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if not (path == "/video" or path.startswith("/video/")):
        return False
    return host == "bilibili.com" or host.endswith(".bilibili.com") or host == "douyin.com" or host.endswith(".douyin.com")


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
    # 稳定目录型源（如微信读书书架）的条目会长期留在源列表里，再次抓到不代表没处理完，
    # 重命中时保留完成态；普通「消费即移出」的源保持默认 False（重命中即复活）。
    keep_finished: bool = False


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
            if "digest_status" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN digest_status TEXT NOT NULL DEFAULT 'pending'")
            if "digest_path" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN digest_path TEXT")
            if "digest_at" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN digest_at TEXT")
            if "digest_error" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN digest_error TEXT")
            if "digest_attempts" not in columns:
                conn.execute("ALTER TABLE links ADD COLUMN digest_attempts INTEGER NOT NULL DEFAULT 0")
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_links_digest_status ON links(digest_status)")

    def upsert_link(self, record: LinkRecord) -> int:
        now = self._now_iso()
        keep = 1 if record.keep_finished else 0
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
                        -- 普通源：抓取又抓到 = 源列表里还在 = 没处理完，完成态清零，读态一起清，
                        -- 回到未读样式的待办状态；CASE 读的是更新前的值，故只影响「本来就是完成」的行。
                        -- keep_finished 源（稳定目录）：条目长期在源里，完成态由用户决定，重命中不清。
                        is_opened = CASE WHEN ? = 0 AND is_finished = 1 THEN 0 ELSE is_opened END,
                        opened_at = CASE WHEN ? = 0 AND is_finished = 1 THEN NULL ELSE opened_at END,
                        is_finished = CASE WHEN ? = 1 THEN is_finished ELSE 0 END,
                        finished_at = CASE WHEN ? = 1 THEN finished_at ELSE NULL END,
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
                        keep,
                        keep,
                        keep,
                        keep,
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

    def find_url(self, url: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()
        return int(row["id"]) if row else None

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

    # ---- 视频简报（digest）状态机 ----

    def list_digest_candidates(
        self, *, limit: int, source: str | None = None, retry_failed: bool = False
    ) -> list[sqlite3.Row]:
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        placeholders = ", ".join("?" for _ in statuses)
        sql = f"SELECT * FROM links WHERE {VIDEO_URL_CONDITION} AND digest_status IN ({placeholders})"
        params: list = list(statuses)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return list(rows)

    def claim_digest(self, link_id: int, *, retry_failed: bool = False) -> bool:
        allowed = ("pending", "failed") if retry_failed else ("pending",)
        placeholders = ", ".join("?" for _ in allowed)
        now = self._now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE links
                SET digest_status = 'running',
                    digest_attempts = digest_attempts + 1,
                    digest_error = NULL,
                    updated_at = ?
                WHERE id = ?
                  AND digest_status IN ({placeholders})
                """,
                (now, link_id, *allowed),
            )
            return cursor.rowcount == 1

    def mark_digest_succeeded(self, link_id: int, *, digest_path: str, completed_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET digest_status = 'succeeded',
                    digest_path = ?,
                    digest_at = ?,
                    digest_error = NULL
                WHERE id = ?
                """,
                (digest_path, completed_at, link_id),
            )

    def mark_digest_failed(self, link_id: int, *, error_message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET digest_status = 'failed',
                    digest_error = ?
                WHERE id = ?
                """,
                (error_message[:500], link_id),
            )

    def mark_digest_unsupported(self, link_id: int, *, error_message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET digest_status = 'unsupported',
                    digest_error = ?
                WHERE id = ?
                """,
                (error_message, link_id),
            )

    def reset_unsupported_to_pending(self) -> int:
        """把视频条目里 unsupported 的行重置为 pending，供抖音链路解锁后重跑。"""
        now = self._now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE links
                SET digest_status = 'pending',
                    digest_error = NULL,
                    updated_at = ?
                WHERE {VIDEO_URL_CONDITION}
                  AND digest_status = 'unsupported'
                """,
                (now,),
            )
            return int(cursor.rowcount)

    def recover_stale_digest_runs(self, *, stale_seconds: int = 7200) -> int:
        now = datetime.now(UTC)
        threshold = (now - timedelta(seconds=stale_seconds)).isoformat(timespec="seconds")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE links
                SET digest_status = 'pending',
                    digest_error = '上次任务中断，已重新排队',
                    updated_at = ?
                WHERE digest_status = 'running'
                  AND updated_at < ?
                """,
                (now.isoformat(timespec="seconds"), threshold),
            )
            return int(cursor.rowcount)

    def get_digest_row(self, link_id: int) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()

    def get_digest_stats(self) -> dict[str, int]:
        stats = {"pending": 0, "running": 0, "succeeded": 0, "failed": 0, "unsupported": 0}
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT digest_status, COUNT(*) AS n
                FROM links
                WHERE {VIDEO_URL_CONDITION}
                GROUP BY digest_status
                """
            ).fetchall()
        for row in rows:
            if row["digest_status"] in stats:
                stats[row["digest_status"]] = int(row["n"])
        return stats

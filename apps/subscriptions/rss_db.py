from __future__ import annotations

import sqlite3
import re
import sys
from pathlib import Path
from typing import Iterable

from apps.subscriptions.models import FeedEntry
from apps.subscriptions.source_ids import canonicalize_source_id, legacy_source_ids
from connectors._shared.common import normalize_dedupe_title_key, parse_published_datetime
from infra.text_normalizer import normalize_utf8_text


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "subscriptions.sqlite3"

# 订阅库体积上限：未超过时一条历史都不清理，超过后从最旧条目开始删到回落。
MAX_DB_BYTES = 10 * 1024 ** 3

# 一个页面 URL 聚合多条条目的源：link 不唯一，不能参与「按 link 去重 / link 唯一索引」。
# 蓝宝书详情页与社媒默认页都是这种形态（一条 link 对应多张卡片）。
SHARED_PAGE_SOURCE_IDS = frozenset({"alphapai", "alphapai-social-media"})
_SHARED_PAGE_SQL = "('" + "', '".join(sorted(SHARED_PAGE_SOURCE_IDS)) + "')"


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


_SCHEMA_READY_PATHS: set[str] = set()


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # ensure_schema 含逐次全表去重维护(自连接 DELETE + 索引重建),对已就绪的库
    # 每次连接重跑是纯浪费(每次刷新 0.4s/连接)。首次就绪后进程内跳过维护段,
    # 结构缺列/缺索引时仍由内部 IF NOT EXISTS / ADD COLUMN 兜底补齐。
    # 按 DB 路径记录就绪态,避免测试替换 DB_PATH 后跳对真实库的维护。
    if str(DB_PATH) not in _SCHEMA_READY_PATHS:
        ensure_schema(conn)
        _SCHEMA_READY_PATHS.add(str(DB_PATH))
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
        f"""
        DELETE FROM rss_entries
        WHERE id IN (
            SELECT older.id
            FROM rss_entries AS older
            JOIN rss_entries AS newer
              ON older.link = newer.link
             AND older.source_id NOT IN {_SHARED_PAGE_SQL}
             AND newer.source_id NOT IN {_SHARED_PAGE_SQL}
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
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_entries_link_unique_non_alphapai
        ON rss_entries(link)
        WHERE source_id NOT IN {_SHARED_PAGE_SQL}
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
    # 去重口径唯一实现收敛到 common（connector 与应用层共用同一份）。
    return normalize_dedupe_title_key(text)


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

            if source_id in SHARED_PAGE_SOURCE_IDS:
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
                ON CONFLICT DO UPDATE SET
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
        prune_entries_over_limit(conn)
    finally:
        conn.close()
    return inserted


def _db_file_size() -> int:
    try:
        return DB_PATH.stat().st_size
    except OSError:
        return 0


def _vacuum(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("VACUUM")
        return True
    except sqlite3.Error:
        return False


def prune_entries_over_limit(conn: sqlite3.Connection, limit_bytes: int | None = None) -> int:
    """订阅库超过体积上限时，从最旧条目开始删除并回收空间，返回删除条数。

    体积按库文件大小计。删除只释放页、不缩文件，必须 VACUUM 才真正回落，
    故 VACUUM 失败即停止本轮——否则会"删了行体积不降、下轮接着删"，越删越少。
    """
    limit = MAX_DB_BYTES if limit_bytes is None else int(limit_bytes)
    deleted = 0
    try:
        while True:
            size = _db_file_size()
            if size <= limit:
                break
            total = int(conn.execute("SELECT COUNT(*) FROM rss_entries").fetchone()[0])
            if total <= 0:
                break
            # 按当前平均行体积估算要删多少行，多删 1 行留余量
            per_row = max(1, size // total)
            batch = min(total, (size - limit) // per_row + 1)
            conn.execute(
                """
                DELETE FROM rss_entries
                WHERE id IN (
                    SELECT id FROM rss_entries
                    ORDER BY COALESCE(NULLIF(published_at, ''), NULLIF(published, ''), created_at) ASC, id ASC
                    LIMIT ?
                )
                """,
                (batch,),
            )
            conn.commit()
            deleted += batch
            if not _vacuum(conn):
                print(f"[subscriptions] 订阅库清理中止：已删 {deleted} 条但 VACUUM 失败，体积未回落", file=sys.stderr)
                break
    except sqlite3.Error as exc:
        print(f"[subscriptions] 订阅库清理中止：{exc}", file=sys.stderr)
    return deleted


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

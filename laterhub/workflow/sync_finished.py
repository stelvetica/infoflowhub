from __future__ import annotations

import argparse
import sqlite3

from laterhub.services.config import DB_PATH, ENV_PATH
from laterhub.services.feishu import FeishuBitableClient, FeishuConfig
from laterhub.storage.db import DBManager


FIELD_LINK = "链接"
FIELD_FINISHED = "已看完"
FIELD_TITLE = "标题"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只同步飞书中已看完勾选的记录回本地数据库")
    parser.add_argument("--page-size", type=int, default=100, help="每次从飞书读取的分页大小，默认 100")
    return parser.parse_args()


def extract_url(fields: dict) -> str:
    link_field = fields.get(FIELD_LINK)
    if isinstance(link_field, dict):
        return (link_field.get("link") or "").split("?")[0].strip()
    if isinstance(link_field, str):
        return link_field.split("?")[0].strip()
    return ""


def main() -> None:
    args = parse_args()
    client = FeishuBitableClient(FeishuConfig.from_env(ENV_PATH))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    page_token = None
    fetched = 0
    matched = 0
    updated = 0
    skipped = 0
    filter_expr = f"CurrentValue.[{FIELD_FINISHED}] = 1"

    while True:
        data = client.list_records(page_token=page_token, page_size=args.page_size, filter_expr=filter_expr)
        items = (((data.get("data") or {}).get("items")) or [])
        fetched += len(items)

        for item in items:
            fields = item.get("fields") or {}
            url = extract_url(fields)
            if not url:
                skipped += 1
                continue

            row = conn.execute("SELECT id, is_finished FROM links WHERE url = ?", (url,)).fetchone()
            if not row:
                skipped += 1
                continue

            matched += 1
            if int(row["is_finished"] or 0) == 1:
                conn.execute(
                    "UPDATE links SET feishu_last_synced_at = ?, updated_at = ? WHERE id = ?",
                    (DBManager.now_iso(), DBManager.now_iso(), row["id"]),
                )
                continue

            conn.execute(
                """
                UPDATE links
                SET is_finished = 1,
                    finished_at = COALESCE(finished_at, ?),
                    feishu_last_synced_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (DBManager.now_iso(), DBManager.now_iso(), DBManager.now_iso(), row["id"]),
            )
            updated += 1
            title = str(fields.get(FIELD_TITLE) or "")
            print(f"[SYNCED] id={row['id']} title={title[:60]} url={url}")

        page_token = ((data.get("data") or {}).get("page_token"))
        has_more = bool((data.get("data") or {}).get("has_more"))
        if not has_more or not page_token:
            break

    conn.commit()
    conn.close()
    print(f"done fetched={fetched} matched={matched} updated={updated} skipped={skipped}")

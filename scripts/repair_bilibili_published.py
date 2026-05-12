from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from connectors.bilibili.api import fetch_bilibili_user_dynamic

DB_PATH = BASE_DIR / "data" / "subscriptions.sqlite3"


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        sources = conn.execute(
            """
            SELECT DISTINCT source_id, source_name, link
            FROM rss_entries
            WHERE source_name LIKE '%bilibili%'
            """
        ).fetchall()

        source_uid_map: dict[str, str] = {}
        for row in sources:
            source_id = str(row["source_id"])
            if source_id in source_uid_map:
                continue
            link = str(row["link"] or "")
            uid = ""
            if "space.bilibili.com/" in link:
                parts = link.split("space.bilibili.com/", 1)[1].split("/", 1)
                uid = parts[0].strip()
            if uid:
                source_uid_map[source_id] = uid

        # 从配置名无法反推 uid，改为从配置表读取站点 URL
        config_path = BASE_DIR / "config" / "rss_sources.json"
        if config_path.exists():
            import json
            import re

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            for item in payload.get("sources", []):
                source_id = str(item.get("id") or "")
                site_url = str(item.get("site_url") or "")
                match = re.search(r"space\.bilibili\.com/(\d+)", site_url)
                if source_id and match:
                    source_uid_map[source_id] = match.group(1)

        updated = 0
        for source_id, uid in source_uid_map.items():
            items = fetch_bilibili_user_dynamic(uid, limit=60, max_pages=6, timeout=20)
            by_link = {}
            for item in items:
                for key in filter(None, [item.get("link"), item.get("dynamic_link")]):
                    by_link[str(key).strip()] = str(item.get("published_at") or "").strip()

            if not by_link:
                continue

            rows = conn.execute(
                """
                SELECT id, link, published
                FROM rss_entries
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchall()
            for row in rows:
                link = str(row["link"] or "").strip()
                published = by_link.get(link, "").strip()
                if not published or published == str(row["published"] or "").strip():
                    continue
                conn.execute(
                    """
                    UPDATE rss_entries
                    SET published = ?
                    WHERE id = ?
                    """,
                    (published, int(row["id"])),
                )
                updated += 1

        conn.commit()
        print(updated)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

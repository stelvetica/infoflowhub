from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from apps.subscriptions.config import load_settings, load_sources
from apps.subscriptions.rss_db import delete_entries_by_source, delete_source_state, get_connection, list_source_stats, save_entries, set_source_enabled
from connectors.rss.fetch import fetch_many
from apps.laterhub.config import DB_PATH as LATERHUB_DB_PATH

RUNTIME_DIR = BASE_DIR / "runtime"
STATUS_PATH = RUNTIME_DIR / "health" / "subscriptions_status.json"
HEALTH_PATH = RUNTIME_DIR / "health" / "subscriptions_source_health.json"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: Path, default: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_status() -> dict:
    return load_json(
        STATUS_PATH,
        {
            "last_run_at": "",
            "last_success_at": "",
            "last_error": "",
            "last_total_sources": 0,
            "last_success_sources": 0,
            "last_inserted_entries": 0,
        },
    )


def save_status(status: dict) -> None:
    save_json(STATUS_PATH, status)


def load_health() -> dict:
    return load_json(HEALTH_PATH, {"sources": {}})


def save_health(health: dict) -> None:
    save_json(HEALTH_PATH, health)


def clean_health_error(value: str) -> str:
    text = " ".join((value or "").strip().split())
    return text[:320]


def update_source_health(result) -> None:
    health = load_health()
    source_health = health.setdefault("sources", {})
    current = source_health.get(result.source_id, {})
    current["source_name"] = result.source_name
    current["feed_url"] = result.feed_url
    current["last_checked_at"] = now_text()
    if result.ok:
        current["last_success_at"] = current["last_checked_at"]
        current["last_error"] = ""
        current["last_failed_at"] = current.get("last_failed_at", "")
    else:
        current["last_error"] = clean_health_error(result.error or str(result.status))
        current["last_failed_at"] = current["last_checked_at"]
    source_health[result.source_id] = current
    save_health(health)


def fetch_now() -> dict:
    sources = [item for item in load_sources() if item.get("enabled", False)]
    settings = load_settings()
    results = fetch_many(sources, settings=settings, timeout=45)

    inserted_total = 0
    success_sources = 0
    failures: list[str] = []

    for result in results:
        update_source_health(result)
        if not result.ok:
            failures.append(f"{result.source_name}: {result.error or result.status}")
            continue
        success_sources += 1
        inserted_total += save_entries(result.entries)

    status = load_status()
    status["last_run_at"] = now_text()
    status["last_success_at"] = status["last_run_at"] if success_sources else status.get("last_success_at", "")
    status["last_total_sources"] = len(sources)
    status["last_success_sources"] = success_sources
    status["last_inserted_entries"] = inserted_total
    status["last_error"] = " | ".join(failures[:10])
    save_status(status)
    return status


def list_entries(limit: int = 500) -> list[dict]:
    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            """
            SELECT source_id, source_name, title, link, published, summary, created_at
            FROM rss_entries
            ORDER BY COALESCE(NULLIF(published, ''), created_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def count_entries() -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) FROM rss_entries").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def list_laterhub_items(limit: int = 500) -> list[dict]:
    if not LATERHUB_DB_PATH.exists():
        return []
    sqlite3 = __import__("sqlite3")
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, url, title, tags, created_at, updated_at, is_finished
            FROM links
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def count_laterhub_items() -> int:
    if not LATERHUB_DB_PATH.exists():
        return 0
    sqlite3 = __import__("sqlite3")
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    try:
        row = conn.execute("SELECT COUNT(*) FROM links").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def list_laterhub_summary() -> dict:
    if not LATERHUB_DB_PATH.exists():
        return {"total_count": 0, "unfinished_count": 0, "finished_count": 0}
    sqlite3 = __import__("sqlite3")
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total_count,
                   SUM(CASE WHEN is_finished = 1 THEN 1 ELSE 0 END) AS finished_count,
                   SUM(CASE WHEN is_finished = 0 THEN 1 ELSE 0 END) AS unfinished_count
            FROM links
            """
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {"total_count": 0, "unfinished_count": 0, "finished_count": 0}


def laterhub_source_meta(source: str) -> dict[str, str]:
    mapping = {
        "bilibili_watchlater": {
            "label": "B站稍后看",
            "purpose": "收藏/稍后处理",
            "fetch_mode": "私有API登录态",
        },
        "douyin_favorite": {
            "label": "抖音收藏",
            "purpose": "收藏/稍后处理",
            "fetch_mode": "私有网页登录态",
        },
        "manual_verify": {
            "label": "人工补录",
            "purpose": "资料核实/待整理",
            "fetch_mode": "人工录入",
        },
    }
    return mapping.get(source, {"label": source, "purpose": "待分类", "fetch_mode": "待识别"})


def list_laterhub_source_stats() -> list[dict]:
    if not LATERHUB_DB_PATH.exists():
        return []
    sqlite3 = __import__("sqlite3")
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT source,
                   COUNT(*) AS total_count,
                   SUM(CASE WHEN is_finished = 1 THEN 1 ELSE 0 END) AS finished_count,
                   SUM(CASE WHEN is_finished = 0 THEN 1 ELSE 0 END) AS unfinished_count
            FROM links
            GROUP BY source
            ORDER BY source COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item.update(laterhub_source_meta(item["source"]))
        result.append(item)
    return result


def mark_laterhub_finished(link_id: int, finished: bool) -> dict:
    if not LATERHUB_DB_PATH.exists():
        return {"ok": True}
    sqlite3 = __import__("sqlite3")
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    try:
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            UPDATE links
            SET is_finished = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (1 if finished else 0, now_iso if finished else None, now_iso, link_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def snapshot() -> dict:
    return {
        "entries": list_entries(500),
        "entries_total": count_entries(),
        "source_stats": list_source_stats(),
        "laterhub_items": list_laterhub_items(500),
        "laterhub_total": count_laterhub_items(),
        "laterhub_summary": list_laterhub_summary(),
        "laterhub_source_stats": list_laterhub_source_stats(),
    }


def entries_snapshot() -> dict:
    return {"entries": list_entries(500), "entries_total": count_entries()}


def laterhub_snapshot() -> dict:
    return {
        "laterhub_items": list_laterhub_items(500),
        "laterhub_total": count_laterhub_items(),
        "laterhub_summary": list_laterhub_summary(),
        "laterhub_source_stats": list_laterhub_source_stats(),
    }


def settings_snapshot() -> dict:
    return {
        "source_stats": list_source_stats(),
        "laterhub_summary": list_laterhub_summary(),
        "laterhub_source_stats": list_laterhub_source_stats(),
    }


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = json.loads(sys.stdin.read() or "{}")
    if command == "fetch-now":
        print(json.dumps(fetch_now(), ensure_ascii=False))
        return 0
    if command == "snapshot":
        print(json.dumps(snapshot(), ensure_ascii=False))
        return 0
    if command == "entries-snapshot":
        print(json.dumps(entries_snapshot(), ensure_ascii=False))
        return 0
    if command == "laterhub-snapshot":
        print(json.dumps(laterhub_snapshot(), ensure_ascii=False))
        return 0
    if command == "settings-snapshot":
        print(json.dumps(settings_snapshot(), ensure_ascii=False))
        return 0
    if command == "mark-laterhub-finished":
        print(json.dumps(mark_laterhub_finished(int(payload.get("id", 0)), bool(payload.get("finished"))), ensure_ascii=False))
        return 0
    if command == "set-source-enabled":
        set_source_enabled(str(payload.get("source_id", "")), bool(payload.get("enabled")))
        print(json.dumps({"ok": True}, ensure_ascii=False))
        return 0
    if command == "delete-source-data":
        source_id = str(payload.get("source_id", ""))
        delete_entries_by_source(source_id)
        delete_source_state(source_id)
        print(json.dumps({"ok": True}, ensure_ascii=False))
        return 0
    print(json.dumps({"error": f"unknown command: {command}"}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

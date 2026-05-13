from __future__ import annotations

from apps.subscriptions.config import load_settings, load_sources
from apps.subscriptions.rss_db import save_entries
from apps.laterhub.config import DB_PATH, ENV_PATH
from apps.laterhub.db import DBManager, LinkRecord
from connectors.bilibili import fetch_bilibili_watchlater
from connectors.douyin import fetch_douyin_favorites
from connectors.rss.fetch import fetch_many

from web.services.views import load_health, load_status, write_json, HEALTH_PATH, STATUS_PATH


def now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_health_error(value: str) -> str:
    return " ".join((value or "").strip().split())[:320]


def is_expected_login_requirement_error(message: str) -> bool:
    text = clean_health_error(message)
    return "依赖本机 Chrome Profile 2 登录态" in text


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
    write_json(HEALTH_PATH, health)


def save_status(status: dict) -> None:
    write_json(STATUS_PATH, status)


def fetch_now() -> dict:
    status = load_status()
    status["fetch_state"] = "running"
    status["current_run_started_at"] = now_text()
    save_status(status)
    sources = [item for item in load_sources() if item.get("enabled", False)]
    settings = load_settings()
    try:
        results = fetch_many(sources, settings=settings, timeout=45)
        inserted_total = 0
        success_sources = 0
        failures: list[str] = []
        warnings: list[str] = []
        for result in results:
            update_source_health(result)
            if not result.ok:
                issue_text = f"{result.source_name}: {result.error or result.status}"
                if is_expected_login_requirement_error(result.error or ""):
                    warnings.append(issue_text)
                    continue
                failures.append(issue_text)
                continue
            success_sources += 1
            inserted_total += save_entries(result.entries)
        status = load_status()
        status["fetch_state"] = "success" if not failures else "error"
        status["last_run_at"] = now_text()
        status["last_success_at"] = status["last_run_at"] if success_sources else status.get("last_success_at", "")
        status["last_total_sources"] = len(sources)
        status["last_success_sources"] = success_sources
        status["last_inserted_entries"] = inserted_total
        status["last_error"] = " | ".join((failures + warnings)[:10])
        save_status(status)
        return status
    except Exception as exc:
        status = load_status()
        status["fetch_state"] = "error"
        status["last_run_at"] = now_text()
        status["last_total_sources"] = len(sources)
        status["last_success_sources"] = 0
        status["last_inserted_entries"] = 0
        status["last_error"] = clean_health_error(str(exc))
        save_status(status)
        return status
    finally:
        status = load_status()
        if status.get("fetch_state") == "running":
            status["fetch_state"] = "idle"
            save_status(status)


def fetch_laterhub_now() -> dict[str, int]:
    db = DBManager(DB_PATH)
    fetched_total = 0
    inserted_total = 0
    for fetcher in (fetch_bilibili_watchlater, fetch_douyin_favorites):
        items = fetcher(ENV_PATH)
        fetched_total += len(items)
        for item in items:
            before_count = len(db.list_by_status("pending", "pushed", "failed"))
            db.upsert_link(
                LinkRecord(
                    url=item["url"],
                    title=item["title"],
                    source=item["source"],
                    tags=item.get("tags"),
                )
            )
            after_count = len(db.list_by_status("pending", "pushed", "failed"))
            if after_count > before_count:
                inserted_total += 1
    return {"fetched_total": fetched_total, "inserted_total": inserted_total}

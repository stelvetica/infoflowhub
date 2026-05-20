from __future__ import annotations

from apps.laterhub.pipeline import run_main_flow
from apps.subscriptions.config import load_settings, load_sources
from apps.subscriptions.rss_db import save_entries
from apps.subscriptions.source_ids import canonicalize_source_id
from connectors.rss.fetch import fetch_many
from web.services.views import HEALTH_PATH, STATUS_PATH, load_health, load_status, write_json


def now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_health_error(value: str) -> str:
    return " ".join((value or "").strip().split())[:320]


def is_expected_login_requirement_error(message: str) -> bool:
    text = clean_health_error(message).lower()
    return "登录态" in text or "login" in text


def is_successful_run(success_sources: int, failure_count: int) -> bool:
    return success_sources > 0 and failure_count == 0


def update_source_health(result) -> None:
    health = load_health()
    source_health = health.setdefault("sources", {})
    source_id = canonicalize_source_id(result.source_id)
    current = source_health.get(source_id, {})
    current["last_checked_at"] = now_text()
    if result.ok:
        current["last_success_at"] = current["last_checked_at"]
        current["last_error"] = ""
        current["last_failed_at"] = current.get("last_failed_at", "")
    else:
        current["last_error"] = clean_health_error(result.error or str(result.status))
        current["last_failed_at"] = current["last_checked_at"]
    source_health[source_id] = {
        "last_checked_at": str(current.get("last_checked_at") or ""),
        "last_success_at": str(current.get("last_success_at") or ""),
        "last_failed_at": str(current.get("last_failed_at") or ""),
        "last_error": str(current.get("last_error") or ""),
    }
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
        run_success = is_successful_run(success_sources, len(failures))
        status["fetch_state"] = "success" if run_success else "error"
        status["last_run_at"] = now_text()
        status["last_success_at"] = status["last_run_at"] if run_success else status.get("last_success_at", "")
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
    result = run_main_flow(fetch_bilibili=True, fetch_douyin=True, retry_failed=False)
    return {
        "fetched_sources": result.fetched_sources,
        "pending_total": result.pending_total,
        "push_enabled": int(result.push_enabled),
    }

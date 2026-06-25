from __future__ import annotations

from apps.laterhub.pipeline import run_main_flow
from apps.subscriptions.config import load_sources
from apps.subscriptions.runtime_health import (
    HEALTH_PATH,
    STATUS_PATH,
    clean_health_error,
    is_expected_login_requirement_error,
    load_health,
    load_status,
    now_text,
    run_source_fetch,
    save_status,
    update_source_health,
    write_json,
)


def is_successful_run(success_sources: int, failure_count: int) -> bool:
    return success_sources > 0 and failure_count == 0


def fetch_now() -> dict:
    sources = [item for item in load_sources() if item.get("enabled", False)]
    outcome = run_source_fetch(sources, timeout=45)
    return outcome.status


def fetch_laterhub_now() -> dict[str, int]:
    result = run_main_flow(fetch_bilibili=True, fetch_douyin=True, retry_failed=False)
    return {
        "fetched_sources": result.fetched_sources,
        "pending_total": result.pending_total,
        "push_enabled": int(result.push_enabled),
    }

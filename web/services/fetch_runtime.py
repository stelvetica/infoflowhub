from __future__ import annotations

from apps.laterhub.pipeline import run_main_flow
from apps.subscriptions.config import load_sources
from apps.subscriptions.runtime_health import (
    run_source_fetch,
)

DEFAULT_PROFILE_DIR = None


def _default_source_profile_dir():
    from pathlib import Path
    return Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"


def is_successful_run(success_sources: int, failure_count: int) -> bool:
    return success_sources > 0 and failure_count == 0


def fetch_now(session=None) -> dict:
    sources = [item for item in load_sources() if item.get("enabled", False)]
    outcome = run_source_fetch(sources, timeout=45, session=session)
    return outcome.status


def fetch_laterhub_now(session=None) -> dict[str, int]:
    result = run_main_flow(fetch_bilibili=True, fetch_douyin=True, fetch_xiaoheihe=True, retry_failed=False, session=session)
    return {
        "fetched_sources": result.fetched_sources,
        "pending_total": result.pending_total,
        "push_enabled": int(result.push_enabled),
    }

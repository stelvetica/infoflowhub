from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.subscriptions.models import FeedFetchResult
from apps.subscriptions.rss_db import save_entries
from apps.subscriptions.source_ids import canonicalize_source_id, merge_source_health_rows
from connectors.rss.fetch import fetch_many
from infra.text_normalizer import normalize_utf8_obj, normalize_utf8_text
from infra.utf8_json import dump_json_utf8, load_json_utf8


BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"
HEALTH_PATH = RUNTIME_DIR / "health" / "subscriptions_source_health.json"
STATUS_PATH = RUNTIME_DIR / "health" / "subscriptions_status.json"


@dataclass
class FetchRunOutcome:
    results: list[FeedFetchResult] = field(default_factory=list)
    inserted_total: int = 0
    inserted_by_source: dict[str, int] = field(default_factory=dict)
    success_sources: int = 0
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)
    fatal_error: str = ""


def now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return load_json_utf8(path)
    except Exception:
        return fallback


def write_json(path: Path, value: Any) -> None:
    dump_json_utf8(path, value)


def load_status() -> dict[str, Any]:
    return read_json(
        STATUS_PATH,
        {
            "fetch_state": "idle",
            "current_run_started_at": "",
            "last_run_at": "",
            "last_success_at": "",
            "last_error": "",
            "last_total_sources": 0,
            "last_success_sources": 0,
            "last_inserted_entries": 0,
        },
    )


def load_health() -> dict[str, Any]:
    health = normalize_utf8_obj(read_json(HEALTH_PATH, {"sources": {}}))
    source_rows = health.get("sources", {})
    if not isinstance(source_rows, dict):
        return {"sources": {}}
    merged_sources = merge_source_health_rows(source_rows.items())
    normalized = {"sources": merged_sources}
    if merged_sources != source_rows:
        write_json(HEALTH_PATH, normalized)
    return normalized


def save_status(status: dict[str, Any]) -> None:
    write_json(STATUS_PATH, status)


def clean_health_error(value: str) -> str:
    return " ".join(normalize_utf8_text(value or "").strip().split())[:320]


def is_expected_login_requirement_error(message: str) -> bool:
    text = clean_health_error(message).lower()
    return "登录态" in text or "login" in text


def is_successful_run(success_sources: int, failure_count: int) -> bool:
    return success_sources > 0 and failure_count == 0


def update_source_health(result: FeedFetchResult) -> None:
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
        "source_name": normalize_utf8_text(result.source_name),
    }
    write_json(HEALTH_PATH, health)


def run_source_fetch(
    sources: list[dict],
    *,
    settings: dict | None = None,
    timeout: int = 45,
    session=None,
) -> FetchRunOutcome:
    status = load_status()
    status["fetch_state"] = "running"
    status["current_run_started_at"] = now_text()
    save_status(status)

    outcome = FetchRunOutcome()
    total_sources = len(sources)
    try:
        results = fetch_many(sources, settings=settings, timeout=timeout, session=session)
        outcome.results = results
        for result in results:
            update_source_health(result)
            if not result.ok:
                issue_text = f"{normalize_utf8_text(result.source_name)}: {normalize_utf8_text(result.error or result.status)}"
                if is_expected_login_requirement_error(result.error or ""):
                    outcome.warnings.append(issue_text)
                    continue
                outcome.failures.append(issue_text)
                continue
            outcome.success_sources += 1
            inserted = save_entries(result.entries)
            outcome.inserted_total += inserted
            outcome.inserted_by_source[result.source_id] = inserted

        status = load_status()
        run_success = is_successful_run(outcome.success_sources, len(outcome.failures))
        status["fetch_state"] = "success" if run_success else "error"
        status["last_run_at"] = now_text()
        status["last_success_at"] = status["last_run_at"] if run_success else status.get("last_success_at", "")
        status["last_total_sources"] = total_sources
        status["last_success_sources"] = outcome.success_sources
        status["last_inserted_entries"] = outcome.inserted_total
        status["last_error"] = " | ".join((outcome.failures + outcome.warnings)[:10])
        save_status(status)
        outcome.status = status
        return outcome
    except Exception as exc:
        status = load_status()
        status["fetch_state"] = "error"
        status["last_run_at"] = now_text()
        status["last_total_sources"] = total_sources
        status["last_success_sources"] = 0
        status["last_inserted_entries"] = 0
        status["last_error"] = clean_health_error(str(exc))
        save_status(status)
        outcome.status = status
        outcome.fatal_error = status["last_error"]
        return outcome
    finally:
        status = load_status()
        if status.get("fetch_state") == "running":
            status["fetch_state"] = "idle"
            save_status(status)

from __future__ import annotations

from apps.laterhub.pipeline import run_main_flow
from apps.subscriptions.config import NEWS_SOURCE_PREFIX, load_sources
from apps.subscriptions.runtime_health import (
    run_source_fetch,
)

def is_successful_run(success_sources: int, failure_count: int) -> bool:
    return success_sources > 0 and failure_count == 0


def fetch_now(session=None) -> dict:
    sources = [item for item in load_sources() if item.get("enabled", False)]
    outcome = run_source_fetch(sources, timeout=45, session=session)
    return outcome.status


def fetch_news_now(session=None) -> dict:
    """只抓「资讯」前缀源（蓝宝书 + 韭研），即公告/新闻 skill 的新闻输入。

    8:10 晨间流程不再全量重抓：其余订阅与稍后读是早读材料，归 7:10 前置槽。
    persist_global=False 让面板的「成功源数」继续反映 7:10 的全量结果，
    不被这里的少量资讯源覆写。
    """
    sources = [
        item
        for item in load_sources()
        if item.get("enabled", False) and str(item.get("name") or "").startswith(NEWS_SOURCE_PREFIX)
    ]
    if not sources:
        return {
            "fetch_state": "error",
            "last_total_sources": 0,
            "last_success_sources": 0,
            "last_inserted_entries": 0,
            "last_error": f"没有 name 以「{NEWS_SOURCE_PREFIX}」开头的启用源",
        }
    outcome = run_source_fetch(sources, timeout=45, session=session, persist_global=False)
    return {
        "fetch_state": "success" if is_successful_run(outcome.success_sources, len(outcome.failures)) else "error",
        "last_total_sources": len(sources),
        "last_success_sources": outcome.success_sources,
        "last_inserted_entries": outcome.inserted_total,
        "last_error": " | ".join((outcome.failures + outcome.warnings)[:10]),
    }


def fetch_laterhub_now(session=None) -> dict[str, int]:
    result = run_main_flow(
        fetch_bilibili=True,
        fetch_douyin=True,
        fetch_xiaoheihe=True,
        fetch_weread=True,
        retry_failed=False,
        session=session,
    )
    return {
        "fetched_sources": result.fetched_sources,
        "pending_total": result.pending_total,
        "push_enabled": int(result.push_enabled),
    }

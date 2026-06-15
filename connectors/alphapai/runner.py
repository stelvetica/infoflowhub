from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from apps.subscriptions.models import FeedFetchResult
from apps.subscriptions.rss_db import save_entries
from connectors._shared.common import result_error
from connectors.alphapai.browser import (
    ALPHAPAI_TARGET_URL,
    close_alphapai_debug_browser,
    connect_over_cdp_endpoint,
    ensure_alphapai_debug_browser,
    find_alphapai_tab_url,
    force_rebuild_alphapai_debug_browser,
)
from connectors.alphapai.feed import fetch_alphapai_with_page, looks_like_login_page


DEBUG_DIR = Path(__file__).resolve().parents[2] / "runtime" / "debug"


def _write_debug(name: str, content: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / name).write_text(content, encoding="utf-8")


def _run_fetch_once(source: dict, *, limit: int, timeout_ms: int) -> FeedFetchResult:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(connect_over_cdp_endpoint())
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = None

            for candidate in context.pages:
                if "alphapai-web.rabyte.cn" in str(candidate.url or ""):
                    page = candidate
                    break

            if page is None:
                page = context.new_page()
                page.goto(find_alphapai_tab_url() or ALPHAPAI_TARGET_URL, wait_until="domcontentloaded", timeout=30000)

            result = fetch_alphapai_with_page(page, source, timeout_ms=timeout_ms, limit=limit)
            if not result.ok:
                try:
                    _write_debug("alphapai_last_error_url.txt", str(page.url or ""))
                    _write_debug("alphapai_last_error_html.html", page.content())
                except Exception:
                    pass
            return result
        finally:
            browser.close()
            close_alphapai_debug_browser()


def _needs_profile_rebuild(result: FeedFetchResult) -> bool:
    error_text = str(result.error or "")
    if result.status == 401:
        return True
    return "登录态失效" in error_text or "登录" in error_text


def fetch_alphapai_source(source: dict, *, limit: int = 12, timeout_ms: int = 120000) -> FeedFetchResult:
    try:
        ensure_alphapai_debug_browser()
    except Exception as exc:
        return result_error(source, f"蓝宝书浏览器准备失败: {exc}")

    try:
        result = _run_fetch_once(source, limit=limit, timeout_ms=timeout_ms)
        if _needs_profile_rebuild(result):
            force_rebuild_alphapai_debug_browser()
            result = _run_fetch_once(source, limit=limit, timeout_ms=timeout_ms)
        return result
    except Exception as exc:
        return result_error(source, f"蓝宝书抓取失败: {exc}")


def fetch_and_save_alphapai(source: dict, *, limit: int = 12, timeout_ms: int = 120000) -> tuple[FeedFetchResult, int]:
    result = fetch_alphapai_source(source, limit=limit, timeout_ms=timeout_ms)
    inserted = save_entries(result.entries) if result.ok else 0
    return result, inserted

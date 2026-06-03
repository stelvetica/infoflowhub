from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

from apps.subscriptions.models import FeedFetchResult
from connectors._shared.common import (
    USER_AGENT,
    is_transient_fetch_error,
    launch_weibo_context,
    launch_x_context,
    resolve_web_target,
    result_error,
    validate_douyin_login_prerequisite,
    validate_x_login_prerequisite,
)
from connectors.auth import get_auth_context_path
from connectors.bilibili import fetch_bilibili_dynamic_feed
from connectors.douyin import fetch_douyin_subscription_with_page
from connectors.douyin.favorites import _resolve_default_browser_executable
from connectors.wechat import fetch_wechat_feed
from connectors.weibo import fetch_weibo_with_page
from connectors.x import fetch_x_with_page
from connectors.youtube import fetch_youtube_with_page

WEB_RETRY_DELAYS = (0.0, 2.0)


def launch_douyin_context(playwright, headless: bool):
    profile_dir = get_auth_context_path("douyin_shared")
    profile_dir.mkdir(parents=True, exist_ok=True)
    _, browser_executable = _resolve_default_browser_executable()
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        executable_path=browser_executable,
        headless=headless,
        args=["--window-size=1440,960"],
        user_agent=USER_AGENT,
    )


def _should_retry_web_result(result: FeedFetchResult) -> bool:
    return (not result.ok) and is_transient_fetch_error(result.error or str(result.status))


def _fetch_web_source_once(playwright, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的网页直抓源")

    if target.site == "bilibili":
        return fetch_bilibili_dynamic_feed(source, limit=limit, timeout_ms=timeout_ms)

    if target.site == "wechat":
        return fetch_wechat_feed(source, limit=limit)

    if target.site == "weibo":
        browser = launch_weibo_context(playwright, headless=False)
        try:
            page = browser.new_page()
            return fetch_weibo_with_page(page, source, timeout_ms=timeout_ms)
        finally:
            browser.close()

    if target.site == "x":
        login_error = validate_x_login_prerequisite(source)
        if login_error:
            return result_error(source, login_error)
        browser = launch_x_context(playwright, headless=True)
        try:
            page = browser.new_page()
            return fetch_x_with_page(page, source, timeout_ms=timeout_ms)
        finally:
            browser.close()

    if target.site == "youtube":
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="en-US", extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
            return fetch_youtube_with_page(page, source, timeout_ms=timeout_ms, limit=limit)
        finally:
            browser.close()

    if target.site == "douyin":
        login_error = validate_douyin_login_prerequisite(source)
        if login_error:
            return result_error(source, login_error)
        browser = launch_douyin_context(playwright, headless=True)
        try:
            page = browser.new_page()
            return fetch_douyin_subscription_with_page(page, source, timeout_ms=timeout_ms, limit=limit)
        finally:
            browser.close()

    return result_error(source, "暂不支持的网页直抓源")


def _fetch_web_source_with_retry(source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    last_result: FeedFetchResult | None = None
    for attempt, delay in enumerate(WEB_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            with sync_playwright() as playwright:
                result = _fetch_web_source_once(playwright, source, limit=limit, timeout_ms=timeout_ms)
        except Exception as exc:
            result = result_error(source, f"缃戦〉鐩存姄澶辫触: {exc}")
        last_result = result
        if not _should_retry_web_result(result) or attempt >= len(WEB_RETRY_DELAYS):
            return result
    return last_result or result_error(source, "缃戦〉鐩存姄澶辫触")


def fetch_web_source(source: dict) -> FeedFetchResult:
    return _fetch_web_source_with_retry(source)


def fetch_web_many(sources: list[dict], limit: int = 12, timeout_ms: int = 60000) -> list[FeedFetchResult]:
    if not sources:
        return []

    results: list[FeedFetchResult] = []
    for source in sources:
        results.append(_fetch_web_source_with_retry(source, limit=limit, timeout_ms=timeout_ms))
    return results

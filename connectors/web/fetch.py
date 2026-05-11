from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

from apps.subscriptions.models import FeedFetchResult
from connectors.web.bilibili import fetch_bilibili_dynamic, fetch_bilibili_dynamic_via_api, fetch_bilibili_dynamic_with_page
from connectors.web.common import (
    USER_AGENT,
    launch_bilibili_context,
    launch_weibo_context,
    launch_x_context,
    resolve_web_target,
    result_error,
)
from connectors.web.weibo import fetch_weibo_with_page
from connectors.web.x import fetch_x_with_page


def fetch_web_source(source: dict) -> FeedFetchResult:
    target = resolve_web_target(source)
    if target and target.site == "bilibili":
        return fetch_bilibili_dynamic(source)
    try:
        with sync_playwright() as playwright:
            if target and target.site == "weibo":
                browser = launch_weibo_context(playwright, headless=False)
                page = browser.new_page()
                result = fetch_weibo_with_page(page, source)
            elif target and target.site == "x":
                browser = launch_x_context(playwright, headless=True)
                page = browser.new_page()
                result = fetch_x_with_page(page, source)
            else:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(user_agent=USER_AGENT)
                result = result_error(source, "暂不支持的网页直抓源")
            browser.close()
            return result
    except Exception as exc:
        return result_error(source, f"网页直抓失败: {exc}")


def fetch_web_many(sources: list[dict], limit: int = 12, timeout_ms: int = 60000) -> list[FeedFetchResult]:
    if not sources:
        return []
    results: list[FeedFetchResult] = []
    try:
        with sync_playwright() as playwright:
            fallback_browser = playwright.chromium.launch(headless=True)
            fallback_page = fallback_browser.new_page(user_agent=USER_AGENT)
            bilibili_context = None
            weibo_context = None
            weibo_page = None
            x_context = None
            x_page = None
            for source in sources:
                target = resolve_web_target(source)
                if not target:
                    results.append(result_error(source, "暂不支持的网页直抓源"))
                    continue
                if target.site == "bilibili":
                    api_result = fetch_bilibili_dynamic_via_api(source, limit=limit, timeout_ms=timeout_ms)
                    if api_result.ok:
                        results.append(api_result)
                        time.sleep(0.8)
                        continue
                    if bilibili_context is None:
                        bilibili_context = launch_bilibili_context(playwright, headless=True)
                    bilibili_page = bilibili_context.new_page()
                    try:
                        fallback_result = fetch_bilibili_dynamic_with_page(
                            bilibili_page, source, limit=limit, timeout_ms=timeout_ms
                        )
                    finally:
                        bilibili_page.close()
                    if not fallback_result.ok:
                        fallback_result.error = f"{api_result.error} | 浏览器兜底: {fallback_result.error}"
                    results.append(fallback_result)
                    time.sleep(1.0)
                    continue
                if target.site == "weibo":
                    if weibo_context is None:
                        weibo_context = launch_weibo_context(playwright, headless=False)
                        weibo_page = weibo_context.new_page()
                    results.append(fetch_weibo_with_page(weibo_page, source, timeout_ms=timeout_ms))
                    continue
                if target.site == "x":
                    if x_context is None:
                        x_context = launch_x_context(playwright, headless=True)
                        x_page = x_context.new_page()
                    results.append(fetch_x_with_page(x_page, source, timeout_ms=timeout_ms))
                    continue
                results.append(result_error(source, "暂不支持的网页直抓源"))
            fallback_browser.close()
            if bilibili_context is not None:
                bilibili_context.close()
            if weibo_context is not None:
                weibo_context.close()
            if x_context is not None:
                x_context.close()
    except Exception as exc:
        for source in sources:
            results.append(
                FeedFetchResult(
                    source_id=source["id"],
                    source_name=source["name"],
                    feed_url=source["feed_url"],
                    ok=False,
                    status=0,
                    entries=[],
                    error=f"网页直抓失败: {exc}",
                )
            )
    return results

from __future__ import annotations

from playwright.sync_api import sync_playwright

from apps.subscriptions.models import FeedFetchResult
from connectors.bilibili import fetch_bilibili_dynamic_feed
from connectors.weibo import fetch_weibo_with_page
from connectors.x import fetch_x_with_page
from connectors._shared.common import USER_AGENT, launch_weibo_context, launch_x_context, resolve_web_target, result_error


def fetch_web_source(source: dict) -> FeedFetchResult:
    target = resolve_web_target(source)
    if target and target.site == "bilibili":
        return fetch_bilibili_dynamic_feed(source)
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
            weibo_context = None
            weibo_page = None
            x_context = None
            x_page = None
            for source in sources:
                try:
                    target = resolve_web_target(source)
                    if not target:
                        results.append(result_error(source, "暂不支持的网页直抓源"))
                        continue
                    if target.site == "bilibili":
                        results.append(fetch_bilibili_dynamic_feed(source, limit=limit, timeout_ms=timeout_ms))
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
                except Exception as exc:
                    results.append(result_error(source, f"网页直抓失败: {exc}"))

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

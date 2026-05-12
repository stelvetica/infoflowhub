from __future__ import annotations

from playwright.sync_api import sync_playwright

from apps.subscriptions.models import FeedFetchResult
from connectors.web.bilibili import fetch_bilibili_dynamic, fetch_bilibili_dynamic_via_api
from connectors.web.common import USER_AGENT, launch_weibo_context, resolve_web_target, result_error
from connectors.web.weibo import fetch_weibo_with_page


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
                return result_error(source, "X 鎶撳彇宸插仠鐢細褰撳墠浠呮敮鎸佸湪鏈満 Chrome 涓汉宸ユ牳楠岋紝鍚庣鑷姩鎶撳彇浼氬亸绂荤湡瀹炴椂闂寸嚎")
            else:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(user_agent=USER_AGENT)
                result = result_error(source, "鏆備笉鏀寔鐨勭綉椤电洿鎶撴簮")
            browser.close()
            return result
    except Exception as exc:
        return result_error(source, f"缃戦〉鐩存姄澶辫触: {exc}")


def fetch_web_many(sources: list[dict], limit: int = 12, timeout_ms: int = 60000) -> list[FeedFetchResult]:
    if not sources:
        return []
    results: list[FeedFetchResult] = []
    try:
        with sync_playwright() as playwright:
            weibo_context = None
            weibo_page = None
            for source in sources:
                target = resolve_web_target(source)
                if not target:
                    results.append(result_error(source, "鏆備笉鏀寔鐨勭綉椤电洿鎶撴簮"))
                    continue
                if target.site == "bilibili":
                    results.append(fetch_bilibili_dynamic_via_api(source, limit=limit, timeout_ms=timeout_ms))
                    continue
                if target.site == "weibo":
                    if weibo_context is None:
                        weibo_context = launch_weibo_context(playwright, headless=False)
                        weibo_page = weibo_context.new_page()
                    results.append(fetch_weibo_with_page(weibo_page, source, timeout_ms=timeout_ms))
                    continue
                if target.site == "x":
                    results.append(
                        result_error(source, "X 鎶撳彇宸插仠鐢細褰撳墠浠呮敮鎸佸湪鏈満 Chrome 涓汉宸ユ牳楠岋紝鍚庣鑷姩鎶撳彇浼氬亸绂荤湡瀹炴椂闂寸嚎")
                    )
                    continue
                results.append(result_error(source, "鏆備笉鏀寔鐨勭綉椤电洿鎶撴簮"))
            if weibo_context is not None:
                weibo_context.close()
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
                    error=f"缃戦〉鐩存姄澶辫触: {exc}",
                )
            )
    return results

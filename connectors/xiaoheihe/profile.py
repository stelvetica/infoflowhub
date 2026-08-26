from __future__ import annotations

import re
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import (
    clean_line,
    now_text,
    parse_published_datetime,
    resolve_web_target,
    result_error,
)

# 用户主页动态（帖子）列表 API：GET /bbs/app/profile/events?list_type=moment&userid=<id>
PROFILE_EVENTS_PATH = "/bbs/app/profile/events"
# 小黑盒用户主页帖子必须登录态才可加载（未登录 API 返回空 moments / 跳登录）。
XIAOHEIHE_LOGIN_HINT = "请运行 login_profiles.py xiaoheihe 扫码登录小黑盒（登录态存入共享 auth profile）。"


def _normalize_xiaoheihe_link(linkid: str) -> str:
    linkid = (linkid or "").strip()
    if not linkid:
        return ""
    return f"https://www.xiaoheihe.cn/app/bbs/link/{linkid}"


def _extract_profile_moments_from_network(
    page, target_url: str, timeout_ms: int, limit: int
) -> tuple[list[dict], str]:
    moments: list[dict] = []
    response_error = ""

    def on_response(response):
        nonlocal response_error
        try:
            url = response.url
            if PROFILE_EVENTS_PATH not in url:
                return
            payload = response.json()
            result = payload.get("result") or {}
            for item in (result.get("moments") or []):
                if isinstance(item, dict):
                    linkid = str(item.get("linkid") or item.get("link_id") or "").strip()
                    if linkid:
                        moments.append(item)
                    if len(moments) >= limit * 2:
                        return
        except Exception as exc:
            response_error = str(exc)

    page.on("response", on_response)
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(5000)
    finally:
        page.remove_listener("response", on_response)
    return moments, response_error


def _normalize_moment_to_entry(source: dict, item: dict) -> FeedEntry | None:
    linkid = str(item.get("linkid") or item.get("link_id") or "").strip()
    link = _normalize_xiaoheihe_link(linkid)
    if not link:
        return None
    raw_title = clean_line(str(item.get("title") or ""))
    if not raw_title:
        return None
    title = re.sub(r"\s+", " ", raw_title).strip()
    summary = clean_line(str(item.get("description") or "")) or title

    published = ""
    create_at = item.get("create_at") or item.get("createAt")
    if create_at is not None:
        try:
            ts = int(create_at)
            if ts > 10**12:
                ts //= 1000
            published = datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M")
        except Exception:
            published = ""

    return FeedEntry(
        source_id=source["id"],
        source_name=source["name"],
        title=title,
        link=link,
        published=published or now_text(),
        summary=summary,
    )


def fetch_xiaoheihe_profile_with_page(page, source: dict, timeout_ms: int = 60000, limit: int = 12) -> FeedFetchResult:
    limit = max(1, min(limit, 12))
    target = resolve_web_target(source)
    if not target or target.site != "xiaoheihe":
        return result_error(source, "暂不支持的小黑盒订阅源")

    try:
        moments, response_error = _extract_profile_moments_from_network(page, target.page_url, timeout_ms, limit)
    except PlaywrightTimeoutError as exc:
        return result_error(source, f"小黑盒订阅抓取超时: {exc}")
    except Exception as exc:
        return result_error(source, f"小黑盒订阅抓取失败: {exc}")

    if response_error and not moments:
        return result_error(source, f"小黑盒订阅时间解析失败: {response_error}")

    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    for item in moments:
        entry = _normalize_moment_to_entry(source, item)
        if not entry or entry.link in seen_links:
            continue
        seen_links.add(entry.link)
        entries.append(entry)
        if len(entries) >= limit:
            break

    if not entries:
        return result_error(source, f"小黑盒主页可访问，但未解析到帖子内容（可能未登录）。{XIAOHEIHE_LOGIN_HINT}")

    entries.sort(key=lambda item: parse_published_datetime(item.published) or datetime.min, reverse=True)
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )
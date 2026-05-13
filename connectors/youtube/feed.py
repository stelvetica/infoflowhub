from __future__ import annotations

import json
import re
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import clean_line, parse_published_datetime, resolve_web_target, result_error


def normalize_youtube_published(text: str) -> str:
    value = clean_line(text).lstrip("•").strip()
    if not value:
        return ""
    if parse_published_datetime(value):
        return value
    return value


def _walk_renderer_items(node):
    if isinstance(node, dict):
        if "gridVideoRenderer" in node:
            yield node["gridVideoRenderer"]
        if "richItemRenderer" in node:
            yield from _walk_renderer_items(node["richItemRenderer"])
        if "content" in node:
            yield from _walk_renderer_items(node["content"])
        for value in node.values():
            yield from _walk_renderer_items(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_renderer_items(item)


def extract_youtube_entries_from_initial_data(page, source: dict, limit: int = 12) -> list[FeedEntry]:
    html = page.content()
    match = re.search(r"var ytInitialData = (\{.*?\});", html, flags=re.S)
    if not match:
        match = re.search(r"window\[['\"]ytInitialData['\"]\]\s*=\s*(\{.*?\});", html, flags=re.S)
    if not match:
        return []
    try:
        payload = json.loads(match.group(1))
    except Exception:
        return []

    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    for renderer in _walk_renderer_items(payload):
        video_id = str(renderer.get("videoId") or "").strip()
        title_runs = ((renderer.get("title") or {}).get("runs") or [])
        title = clean_line(" ".join(str(part.get("text") or "") for part in title_runs))
        if not video_id or not title:
            continue
        link = f"https://www.youtube.com/watch?v={video_id}"
        if link in seen_links:
            continue
        seen_links.add(link)
        published = ""
        published_text = (((renderer.get("publishedTimeText") or {}).get("simpleText")) or "").strip()
        if published_text:
            published = normalize_youtube_published(published_text)
        view_text = (((renderer.get("viewCountText") or {}).get("simpleText")) or "").strip()
        summary = clean_line(" ".join(part for part in (published_text, view_text) if part))
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=published or datetime.now().strftime("%Y/%m/%d %H:%M"),
                summary=summary,
            )
        )
        if len(entries) >= limit:
            break
    return entries


def extract_youtube_entries(page, source: dict, limit: int = 12) -> list[FeedEntry]:
    entries = extract_youtube_entries_from_initial_data(page, source, limit=limit)
    if entries:
        return entries
    items = page.locator('a#video-title-link, a#video-title').evaluate_all(
        """(els, limit) => {
          const seen = new Set();
          const rows = [];
          for (const el of els) {
            if (rows.length >= limit) break;
            const href = el.href || '';
            if (!href || !href.includes('/watch')) continue;
            const title = (el.getAttribute('title') || el.textContent || '').trim();
            if (!title || seen.has(href)) continue;
            seen.add(href);
            const card = el.closest('ytd-rich-item-renderer, ytd-grid-video-renderer, ytd-video-renderer');
            const metaText = card ? (card.innerText || card.textContent || '') : '';
            rows.push({ href, title, metaText });
          }
          return rows;
        }""",
        limit,
    )
    entries: list[FeedEntry] = []
    for item in items:
        meta_lines = [clean_line(line) for line in str(item.get("metaText") or "").splitlines()]
        meta_lines = [line for line in meta_lines if line]
        published = ""
        for line in meta_lines:
            if any(token in line for token in ("ago", "views", "观看次数")):
                published = normalize_youtube_published(line)
                if published:
                    break
        summary = " ".join(meta_lines[:6]).strip()
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=clean_line(str(item.get("title") or "")),
                link=str(item.get("href") or "").strip(),
                published=published or datetime.now().strftime("%Y/%m/%d %H:%M"),
                summary=summary,
            )
        )
        if len(entries) >= limit:
            break
    if entries:
        return entries

    body_text = page.locator("body").inner_text()
    lines = [clean_line(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]
    text_entries: list[FeedEntry] = []
    for index, line in enumerate(lines):
        if not re.fullmatch(r"\d{1,2}:\d{2}", line):
            continue
        if index + 2 >= len(lines):
            continue
        title = clean_line(lines[index + 1])
        views = clean_line(lines[index + 2])
        published = clean_line(lines[index + 3]).lstrip("•").strip() if index + 3 < len(lines) else ""
        if not title or "views" not in views.lower():
            continue
        if published and not any(token in published.lower() for token in ("ago", "hour", "hours", "day", "days", "week", "weeks", "month", "months", "year", "years")):
            continue
        text_entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=target_youtube_channel_link(source),
                published=published or datetime.now().strftime("%Y/%m/%d %H:%M"),
                summary=clean_line(f"{line} {views} {published}".strip()),
            )
        )
        if len(text_entries) >= limit:
            break
    return text_entries


def target_youtube_channel_link(source: dict) -> str:
    target = resolve_web_target(source)
    if not target:
        return str(source.get("site_url") or source.get("feed_url") or "").strip()
    return target.page_url


def fetch_youtube_with_page(page, source: dict, timeout_ms: int = 60000, limit: int = 12) -> FeedFetchResult:
    target = resolve_web_target(source)
    if not target or target.site != "youtube":
        return result_error(source, "暂不支持的 YouTube 网页源")
    try:
        page.goto(target.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(4000)
        try:
            page.locator("ytd-rich-grid-renderer, ytd-grid-renderer, ytd-section-list-renderer").first.wait_for(timeout=12000)
        except Exception:
            pass
        entries = extract_youtube_entries(page, source, limit=limit)
    except PlaywrightTimeoutError as exc:
        return result_error(source, f"YouTube 网页直抓超时: {exc}")
    except Exception as exc:
        return result_error(source, f"YouTube 网页直抓失败: {exc}")

    if not entries:
        return result_error(source, "YouTube 页面可访问，但未解析到可入库内容")
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )

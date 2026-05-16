from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import clean_line, now_text, parse_published_datetime, resolve_web_target, result_error


def normalize_youtube_published(text: str) -> str:
    value = clean_line(text).replace("•", "").strip()
    if not value:
        return ""
    if parse_published_datetime(value):
        return value

    lowered = re.sub(r"^(streamed|premiered|updated)\s+", "", value.lower())
    compact_match = re.search(r"(\d+)\s*(m|h|d|w|mo|y)\s+ago", lowered)
    if compact_match:
        amount = int(compact_match.group(1))
        unit = compact_match.group(2)
        now = datetime.now()
        if unit == "m":
            target = now - timedelta(minutes=amount)
        elif unit == "h":
            target = now - timedelta(hours=amount)
        elif unit == "d":
            target = now - timedelta(days=amount)
        elif unit == "w":
            target = now - timedelta(weeks=amount)
        elif unit == "mo":
            target = now - timedelta(days=amount * 30)
        else:
            target = now - timedelta(days=amount * 365)
        return target.strftime("%Y/%m/%d %H:%M")

    match = re.search(r"(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago", lowered)
    if not match:
        return ""

    amount = int(match.group(1))
    unit = match.group(2)
    now = datetime.now()
    if unit.startswith("minute"):
        target = now - timedelta(minutes=amount)
    elif unit.startswith("hour"):
        target = now - timedelta(hours=amount)
    elif unit.startswith("day"):
        target = now - timedelta(days=amount)
    elif unit.startswith("week"):
        target = now - timedelta(weeks=amount)
    elif unit.startswith("month"):
        target = now - timedelta(days=amount * 30)
    else:
        target = now - timedelta(days=amount * 365)
    return target.strftime("%Y/%m/%d %H:%M")


def extract_video_id_from_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    query_id = parse_qs(parsed.query).get("v", [""])[0].strip()
    if query_id:
        return query_id
    match = re.search(r"/watch\?v=([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    return ""


def _walk_renderer_items(node):
    if isinstance(node, dict):
        for key in ("gridVideoRenderer", "videoRenderer", "reelItemRenderer"):
            if key in node:
                yield node[key]
        if "richItemRenderer" in node:
            yield from _walk_renderer_items(node["richItemRenderer"])
        if "content" in node:
            yield from _walk_renderer_items(node["content"])
        for value in node.values():
            yield from _walk_renderer_items(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_renderer_items(item)


def _entry_from_renderer(source: dict, renderer: dict) -> FeedEntry | None:
    video_id = str(renderer.get("videoId") or "").strip()
    title_runs = ((renderer.get("title") or {}).get("runs") or [])
    title = clean_line(" ".join(str(part.get("text") or "") for part in title_runs))
    if not video_id or not title:
        return None
    published_text = (
        ((renderer.get("publishedTimeText") or {}).get("simpleText"))
        or " ".join(str(part.get("text") or "") for part in ((renderer.get("publishedTimeText") or {}).get("runs") or []))
    ).strip()
    view_text = (
        ((renderer.get("viewCountText") or {}).get("simpleText"))
        or " ".join(str(part.get("text") or "") for part in ((renderer.get("viewCountText") or {}).get("runs") or []))
    ).strip()
    return FeedEntry(
        source_id=source["id"],
        source_name=source["name"],
        title=title,
        link=f"https://www.youtube.com/watch?v={video_id}",
        published=normalize_youtube_published(published_text) or now_text(),
        summary=clean_line(" ".join(part for part in (published_text, view_text) if part)),
    )


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
        entry = _entry_from_renderer(source, renderer)
        if not entry or entry.link in seen_links:
            continue
        seen_links.add(entry.link)
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def extract_youtube_entries_from_lockups(page, source: dict, limit: int = 12) -> list[FeedEntry]:
    rows = page.locator("ytd-rich-item-renderer, ytd-grid-video-renderer, ytd-video-renderer").evaluate_all(
        """(els, limit) => {
          const items = [];
          for (const el of els) {
            if (items.length >= limit) break;
            const root = el.querySelector('.ytLockupViewModelHost') || el;
            const imageLink = root.querySelector('a.ytLockupViewModelContentImage[href*="/watch"]');
            const href = imageLink?.getAttribute('href') || '';
            const classes = root.className || '';
            const contentIdMatch = classes.match(/content-id-([A-Za-z0-9_-]+)/);
            const rawText = (el.innerText || '').trim();
            const lines = rawText.split('\\n').map(x => x.trim()).filter(Boolean);
            items.push({
              href,
              contentId: contentIdMatch ? contentIdMatch[1] : '',
              lines,
              text: rawText,
            });
          }
          return items;
        }""",
        limit,
    )

    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    for item in rows:
        href = str(item.get("href") or "").strip()
        video_id = extract_video_id_from_url(href) or str(item.get("contentId") or "").strip()
        if not video_id:
            continue
        link = f"https://www.youtube.com/watch?v={video_id}"
        if link in seen_links:
            continue

        lines = [clean_line(line) for line in (item.get("lines") or []) if clean_line(line)]
        if len(lines) < 3:
            continue

        title = lines[1]
        published = ""
        summary_parts: list[str] = []
        for line in lines[2:]:
            normalized = normalize_youtube_published(line)
            if normalized and not published:
                published = normalized
            summary_parts.append(line)
        if not title:
            continue

        seen_links.add(link)
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=published or now_text(),
                summary=clean_line(" ".join(summary_parts[:4])),
            )
        )
        if len(entries) >= limit:
            break
    return entries


def extract_youtube_entries(page, source: dict, limit: int = 12) -> list[FeedEntry]:
    for extractor in (extract_youtube_entries_from_initial_data, extract_youtube_entries_from_lockups):
        entries = extractor(page, source, limit=limit)
        if entries:
            return entries
    return []


def fetch_youtube_with_page(page, source: dict, timeout_ms: int = 60000, limit: int = 12) -> FeedFetchResult:
    limit = max(1, min(limit, 12))
    target = resolve_web_target(source)
    if not target or target.site != "youtube":
        return result_error(source, "暂不支持的 YouTube 网页源")
    try:
        page.goto(target.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(5000)
        try:
            page.locator("ytd-rich-grid-renderer, ytd-section-list-renderer, ytd-rich-item-renderer").first.wait_for(timeout=15000)
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

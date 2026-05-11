from __future__ import annotations

import json
import re

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors.web.common import fallback_published, normalize_english_date, resolve_web_target, result_error


YOUTUBE_PREFIX = "var ytInitialData = "


def _extract_initial_data(html: str) -> dict | None:
    index = html.find(YOUTUBE_PREFIX)
    if index < 0:
        return None
    start = index + len(YOUTUBE_PREFIX)
    end = html.find(";</script>", start)
    if end < 0:
        return None
    try:
        return json.loads(html[start:end])
    except Exception:
        return None


def _iter_video_items(payload):
    if isinstance(payload, dict):
        if "lockupViewModel" in payload:
            yield payload["lockupViewModel"]
        for value in payload.values():
            yield from _iter_video_items(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_video_items(item)


def _pick_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if "content" in value and isinstance(value["content"], str):
            return value["content"].strip()
        if "simpleText" in value and isinstance(value["simpleText"], str):
            return value["simpleText"].strip()
        if "text" in value and isinstance(value["text"], str):
            return value["text"].strip()
        if "runs" in value and isinstance(value["runs"], list):
            parts = [_pick_text(item) for item in value["runs"]]
            return "".join(part for part in parts if part).strip()
    if isinstance(value, list):
        parts = [_pick_text(item) for item in value]
        return "".join(part for part in parts if part).strip()
    return ""


def _parse_video_item(source: dict, item: dict) -> FeedEntry | None:
    content_id = (item.get("contentId") or "").strip()
    if not content_id:
        return None
    title = _pick_text(item.get("title"))
    if not title:
        return None
    metadata_rows = (((item.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or [])
    published = ""
    summary_parts: list[str] = []
    for row in metadata_rows:
        for part in row.get("metadataParts") or []:
            text = _pick_text(part.get("text"))
            if not text:
                continue
            if not published and re.search(r"(ago$)|(^\d{1,2}\s+\w+\s+ago$)|(^\d+\w+\s+ago$)", text, re.IGNORECASE):
                published = text
                continue
            summary_parts.append(text)
    return FeedEntry(
        source_id=source["id"],
        source_name=source["name"],
        title=title,
        link=f"https://www.youtube.com/watch?v={content_id}",
        published=fallback_published(normalize_english_date(published) or published),
        summary=" | ".join(summary_parts[:3]),
    )


def fetch_youtube_with_page(page, source: dict, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的 YouTube 网页源")
    try:
        page.goto(target.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)
        html = page.content()
        data = _extract_initial_data(html)
        if not data:
            return result_error(source, "YouTube 页面缺少初始数据")
        entries: list[FeedEntry] = []
        seen_links: set[str] = set()
        for item in _iter_video_items(data):
            entry = _parse_video_item(source, item)
            if not entry or entry.link in seen_links:
                continue
            seen_links.add(entry.link)
            entries.append(entry)
            if len(entries) >= limit:
                break
        if not entries:
            return result_error(source, "YouTube 页面可访问，但未解析到视频")
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=target.page_url,
            ok=True,
            status=200,
            entries=entries,
            error="",
        )
    except Exception as exc:
        return result_error(source, f"YouTube 网页直抓失败: {exc}")

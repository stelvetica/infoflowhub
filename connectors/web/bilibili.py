from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors.web.common import (
    USER_AGENT,
    clean_line,
    fallback_published,
    normalize_relative_date,
    normalize_title_key,
    normalize_yearless_date,
    resolve_web_target,
    result_error,
)


def parse_card_text(raw_text: str) -> tuple[str, str]:
    lines = [clean_line(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "", ""

    skip_exact = {"程序员", "充电专属", "热点深度观察", "知识分享官", "投稿了视频", "视频"}
    skip_patterns = [
        r"^\d{2}:\d{2}$",
        r"^[\d.]+[万亿]?$",
        r"^\d{4}\D\d{1,2}\D\d{1,2}\D*投稿了视频$",
        r"^\d{1,2}\D\d{1,2}\D*投稿了视频$",
        r"^.+投稿了视频$",
    ]

    title = ""
    title_index = 0
    for idx, line in enumerate(lines):
        if line in skip_exact:
            continue
        if any(re.fullmatch(pattern, line) for pattern in skip_patterns):
            continue
        if len(line) <= 2:
            continue
        title = line
        title_index = idx
        break
    if not title:
        return "", ""

    summary_parts: list[str] = []
    for line in lines[title_index + 1:]:
        if line in skip_exact:
            continue
        if any(re.fullmatch(pattern, line) for pattern in skip_patterns):
            continue
        summary_parts.append(line)
    return title, " ".join(summary_parts[:4]).strip()


def canonicalize_bilibili_video_link(link: str) -> str:
    raw = (link or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    if "/video/" in path:
        path = f"{path}/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def build_bilibili_source_aliases(source_name: str) -> set[str]:
    raw = clean_line(source_name)
    if not raw:
        return set()
    suffixes = [" 的 bilibili 动态", " bilibili 动态"]
    for suffix in suffixes:
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].strip()
            break
    parts = [part for part in raw.split() if part]
    aliases = {raw}
    if parts:
        aliases.add(parts[-1])
    return {normalize_title_key(item) for item in aliases if item}


def is_bilibili_date_line(line: str) -> bool:
    text = clean_line(line)
    if not text:
        return False
    return bool(
        re.fullmatch(r"\d{4}\D\d{1,2}\D\d{1,2}\D*", text)
        or re.fullmatch(r"\d{1,2}\D\d{1,2}\D*", text)
        or re.fullmatch(r"\d+\s*天前(?:\s+\d{2}:\d{2})?", text)
        or re.fullmatch(r"\d+\s*小时前", text)
        or re.fullmatch(r"\d+\s*分钟前", text)
        or re.fullmatch(r"昨天(?:\s+\d{2}:\d{2})?", text)
        or re.fullmatch(r"前天(?:\s+\d{2}:\d{2})?", text)
        or "投稿了视频" in text
        or "投稿了文章" in text
    )


def normalize_bilibili_date_line(line: str) -> str:
    text = clean_line(line)
    if "投稿了" in text:
        text = text.split("投稿了", 1)[0].strip()
    return normalize_relative_date(text) or normalize_yearless_date(text)


def extract_bilibili_published(raw_text: str) -> str:
    lines = [clean_line(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    for line in lines[:14]:
        if "投稿了视频" in line or "投稿了文章" in line:
            return normalize_bilibili_date_line(line)
        normalized = normalize_relative_date(line)
        if normalized:
            return normalized
    return ""


def parse_bilibili_body_cards(body_text: str, limit: int = 12) -> list[dict]:
    lines = [clean_line(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]
    cards: list[dict] = []
    current_published = ""
    skip_titles = {"充电专属", "投稿了视频", "投稿了文章", "关注", "转发", "点赞", "评论", "全文", "视频"}

    for idx, line in enumerate(lines):
        if is_bilibili_date_line(line):
            current_published = normalize_bilibili_date_line(line)
            continue
        if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", line):
            continue

        title = ""
        summary_parts: list[str] = []
        for j in range(idx + 1, min(idx + 10, len(lines))):
            candidate = lines[j]
            if is_bilibili_date_line(candidate):
                break
            if candidate in skip_titles:
                continue
            if re.fullmatch(r"[\d.]+[万亿]?", candidate):
                continue
            if not title and len(candidate) > 3:
                title = candidate
                continue
            if title:
                summary_parts.append(candidate)
        if title:
            cards.append(
                {
                    "title": title,
                    "published": current_published,
                    "summary": " ".join(summary_parts[:4]).strip(),
                }
            )
        if len(cards) >= limit:
            break
    return cards


def fetch_bilibili_dynamic_with_page(page, source: dict, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的网页直抓站点")

    try:
        page.goto(target.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(8000)
        body_text = page.locator("body").inner_text()
        body_cards = parse_bilibili_body_cards(body_text, limit=limit)
        cards = []
        selectors = [
            "a.bili-dyn-card-video",
            '.bili-dyn-list__item a[href*="/video/"]',
            'a[href*="/video/"]',
        ]
        for selector in selectors:
            cards = page.locator(selector).evaluate_all(
                f"""els => els.slice(0, {limit}).map(el => {{
                    const item = el.closest('.bili-dyn-list__item');
                    return {{
                        text: (el.innerText || el.textContent || '').trim(),
                        href: el.href || '',
                        published_text: item ? (item.innerText || item.textContent || '').trim() : ''
                    }};
                }})"""
            )
            cards = [item for item in cards if item.get("href")]
            if cards:
                break
        if not cards:
            video_page_url = f"https://space.bilibili.com/{target.uid}/video"
            page.goto(video_page_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(5000)
            cards = page.locator('a[href*="/video/"]').evaluate_all(
                f"""els => els.slice(0, {limit * 3}).map(el => {{
                    const card = el.closest('[class*="video"], li, .small-item, .cover-card, .list-item') || el.parentElement;
                    return {{
                        text: ((card && (card.innerText || card.textContent)) || el.innerText || el.textContent || '').trim(),
                        href: el.href || '',
                        published_text: ((card && (card.innerText || card.textContent)) || '').trim()
                    }};
                }})"""
            )
            deduped_cards = []
            seen = set()
            for item in cards:
                href = (item.get("href") or "").strip()
                if not href or "/video/" not in href or href in seen:
                    continue
                seen.add(href)
                deduped_cards.append(item)
                if len(deduped_cards) >= limit:
                    break
            cards = deduped_cards
    except PlaywrightTimeoutError as exc:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source["feed_url"],
            ok=False,
            status=0,
            entries=[],
            error=f"网页直抓超时: {exc}",
        )
    except Exception as exc:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source["feed_url"],
            ok=False,
            status=0,
            entries=[],
            error=f"网页直抓失败: {exc}",
        )

    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    source_aliases = build_bilibili_source_aliases(source["name"])
    body_card_map = {normalize_title_key(item["title"]): item for item in body_cards}
    for item in cards:
        link = canonicalize_bilibili_video_link(item.get("href") or "")
        if not link or link in seen_links:
            continue
        title, summary = parse_card_text(item.get("text", ""))
        if not title:
            continue
        title_key = normalize_title_key(title)
        if title_key in source_aliases:
            continue
        seen_links.add(link)
        published = extract_bilibili_published(item.get("published_text", "") or item.get("text", ""))
        body_match = body_card_map.get(title_key)
        if not published and body_match:
            published = body_match.get("published", "")
        if body_match and not summary:
            summary = body_match.get("summary", "")
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=fallback_published(published),
                summary=summary,
            )
        )

    if not entries and body_cards:
        for item in body_cards[:limit]:
            entries.append(
                FeedEntry(
                    source_id=source["id"],
                    source_name=source["name"],
                    title=item["title"],
                    link=f"https://space.bilibili.com/{target.uid}/dynamic",
                    published=fallback_published(item["published"]),
                    summary=item["summary"],
                )
            )

    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=bool(entries),
        status=200 if entries else 0,
        entries=entries,
        error="" if entries else "网页可访问，但未解析到内容",
    )


def fetch_bilibili_dynamic(source: dict, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(user_agent=USER_AGENT)
            result = fetch_bilibili_dynamic_with_page(page, source, limit=limit, timeout_ms=timeout_ms)
            browser.close()
            return result
    except Exception as exc:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source["feed_url"],
            ok=False,
            status=0,
            entries=[],
            error=f"网页直抓失败: {exc}",
        )

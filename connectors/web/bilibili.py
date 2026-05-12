from __future__ import annotations

import os
import re
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors.api.bilibili import fetch_bilibili_user_dynamic, load_project_env
from connectors.web.common import (
    clean_line,
    fallback_published,
    normalize_relative_date,
    normalize_title_key,
    normalize_yearless_date,
    resolve_web_target,
    result_error,
)


BILIBILI_NAVIGATION_PAUSE_MS = 1200
BILIBILI_RETRY_DELAYS_MS = (1800, 4200, 8000)
BILIBILI_PAGE_CANDIDATES = ("dynamic", "video", "home")


def parse_bilibili_cookie_header() -> list[dict]:
    load_project_env()
    cookie_header = os.getenv("BILIBILI_COOKIE", "").strip()
    if not cookie_header:
        return []
    parsed = SimpleCookie()
    parsed.load(cookie_header)
    cookies: list[dict] = []
    for morsel in parsed.values():
        cookies.append(
            {
                "name": morsel.key,
                "value": morsel.value,
                "domain": ".bilibili.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


def ensure_bilibili_context_ready(context) -> None:
    if getattr(context, "_infoflowhub_bili_ready", False):
        return
    context.set_extra_http_headers(
        {
            "Referer": "https://space.bilibili.com/",
            "Origin": "https://space.bilibili.com",
        }
    )
    context.set_default_navigation_timeout(60000)
    context.set_default_timeout(60000)
    context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in {"image", "media", "font"}
        else route.continue_(),
    )
    cookies = parse_bilibili_cookie_header()
    if cookies:
        context.add_cookies(cookies)
    context._infoflowhub_bili_ready = True


def classify_bilibili_error(exc: Exception | str) -> str:
    message = str(exc)
    upper = message.upper()
    if "ERR_NETWORK_IO_SUSPENDED" in upper:
        return "network_io_suspended"
    if "TIMEOUT" in upper:
        return "timeout"
    if "ERR_ABORTED" in upper:
        return "aborted"
    if "ERR_NAME_NOT_RESOLVED" in upper:
        return "dns"
    if "ERR_CONNECTION" in upper:
        return "connection"
    return "unknown"


def format_bilibili_error(*, target, stage: str, kind: str, url: str, attempt: int, detail: str) -> str:
    compact_detail = clean_line(detail)[:220]
    return (
        f"B?????[uid={target.uid}][stage={stage}][kind={kind}]"
        f"[attempt={attempt}][url={url}]: {compact_detail}"
    )


def build_entries_from_bilibili_api(source: dict, target, limit: int, timeout_ms: int) -> FeedFetchResult:
    items = fetch_bilibili_user_dynamic(target.uid, limit=limit, timeout=max(10, timeout_ms // 1000))
    entries: list[FeedEntry] = []
    for item in items:
        title = clean_line(item.get("title", ""))
        if not title:
            continue
        link = str(item.get("link") or item.get("dynamic_link") or target.page_url).strip()
        summary = clean_line(item.get("summary", ""))
        published = clean_line(item.get("published_at") or item.get("published_text", ""))
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=fallback_published(normalize_bilibili_date_line(published) or published),
                summary=summary,
            )
        )
    if not entries:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=target.page_url,
            ok=False,
            status=0,
            entries=[],
            error=format_bilibili_error(
                target=target,
                stage="api",
                kind="empty",
                url=target.page_url,
                attempt=1,
                detail="???????????????",
            ),
        )
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )


def fetch_bilibili_dynamic_via_api(source: dict, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
        return result_error(source, "????? B ????")
    if not target:
        return result_error(source, "????? B ????")
    try:
        return build_entries_from_bilibili_api(source, target, limit=limit, timeout_ms=timeout_ms)
    except Exception as exc:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=target.page_url,
            ok=False,
            status=0,
            entries=[],
            error=format_bilibili_error(
                target=target,
                stage="api",
                kind=classify_bilibili_error(exc),
                url=target.page_url,
                attempt=1,
                detail=str(exc),
            ),
        )


def wait_before_bilibili_navigation(attempt: int) -> None:
    pause_ms = BILIBILI_NAVIGATION_PAUSE_MS + (attempt - 1) * 400
    time.sleep(pause_ms / 1000)


def extract_bilibili_cards_from_dom(page, limit: int = 12) -> list[dict]:
    selectors = [
        "a.bili-dyn-card-video",
        '.bili-dyn-list__item a[href*="/video/"]',
        '[class*="video"] a[href*="/video/"]',
        'a[href*="/video/"]',
    ]
    for selector in selectors:
        cards = page.locator(selector).evaluate_all(
            f"""els => els.slice(0, {limit * 4}).map(el => {{
                const item =
                    el.closest('.bili-dyn-list__item, [class*="video"], li, .small-item, .cover-card, .list-item') ||
                    el.parentElement;
                return {{
                    text: ((item && (item.innerText || item.textContent)) || el.innerText || el.textContent || '').trim(),
                    href: el.href || '',
                    published_text: item ? (item.innerText || item.textContent || '').trim() : ''
                }};
            }})"""
        )
        cards = [item for item in cards if item.get("href")]
        if cards:
            return cards
    return []


def dedupe_bilibili_cards(cards: list[dict], limit: int) -> list[dict]:
    deduped_cards: list[dict] = []
    seen: set[str] = set()
    for item in cards:
        href = canonicalize_bilibili_video_link(item.get("href") or "")
        if not href or "/video/" not in href or href in seen:
            continue
        seen.add(href)
        deduped_cards.append(
            {
                "text": item.get("text", ""),
                "href": href,
                "published_text": item.get("published_text", ""),
            }
        )
        if len(deduped_cards) >= limit:
            break
    return deduped_cards


def load_bilibili_page_candidates(page, target, limit: int, timeout_ms: int) -> tuple[str, str, list[dict], list[dict], str]:
    candidate_urls = [
        target.page_url,
        f"https://space.bilibili.com/{target.uid}/video",
        f"https://space.bilibili.com/{target.uid}",
    ]
    last_error = ""
    for candidate_index, candidate_url in enumerate(candidate_urls):
        candidate_name = BILIBILI_PAGE_CANDIDATES[candidate_index]
        for attempt, retry_delay_ms in enumerate((0, *BILIBILI_RETRY_DELAYS_MS), start=1):
            if retry_delay_ms:
                page.wait_for_timeout(retry_delay_ms)
            wait_before_bilibili_navigation(attempt)
            try:
                page.goto(candidate_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2500 + candidate_index * 500)
                body_text = page.locator("body").inner_text()
                body_cards = parse_bilibili_body_cards(body_text, limit=limit)
                dom_cards = dedupe_bilibili_cards(extract_bilibili_cards_from_dom(page, limit=limit), limit=limit)
                if body_cards or dom_cards:
                    return candidate_url, body_text, body_cards, dom_cards, ""
                last_error = format_bilibili_error(
                    target=target,
                    stage=f"parse:{candidate_name}",
                    kind="empty",
                    url=candidate_url,
                    attempt=attempt,
                    detail="?????????????",
                )
            except PlaywrightTimeoutError as exc:
                last_error = format_bilibili_error(
                    target=target,
                    stage=f"navigate:{candidate_name}",
                    kind="timeout",
                    url=candidate_url,
                    attempt=attempt,
                    detail=str(exc),
                )
            except PlaywrightError as exc:
                last_error = format_bilibili_error(
                    target=target,
                    stage=f"navigate:{candidate_name}",
                    kind=classify_bilibili_error(exc),
                    url=candidate_url,
                    attempt=attempt,
                    detail=str(exc),
                )
                if "ERR_NETWORK_IO_SUSPENDED" in str(exc).upper():
                    page.wait_for_timeout(1500)
                    try:
                        page.goto("about:blank", wait_until="load", timeout=15000)
                    except Exception:
                        pass
            except Exception as exc:
                last_error = format_bilibili_error(
                    target=target,
                    stage=f"navigate:{candidate_name}",
                    kind=classify_bilibili_error(exc),
                    url=candidate_url,
                    attempt=attempt,
                    detail=str(exc),
                )
    return "", "", [], [], last_error


def parse_card_text(raw_text: str) -> tuple[str, str]:
    lines = [clean_line(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "", ""

    skip_exact = {"???", "????", "??????", "?????", "?????", "??"}
    skip_patterns = [
        r"^\d{2}:\d{2}$",
        r"^\d{2}:\d{2}:\d{2}$",
        r"^[\d.]+[??]?$",
        r"^\d{4}\D\d{1,2}\D\d{1,2}\D*?????$",
        r"^\d{1,2}\D\d{1,2}\D*?????$",
        r"^.+?????$",
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
    for line in lines[title_index + 1 :]:
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
    suffixes = [" ? bilibili ??", " bilibili ??"]
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
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
        return False
    return bool(
        re.fullmatch(r"\d{4}\D\d{1,2}\D\d{1,2}\D*", text)
        or re.fullmatch(r"\d{1,2}\D\d{1,2}\D*", text)
        or re.fullmatch(r"\d+\s*??(?:\s+\d{2}:\d{2})?", text)
        or re.fullmatch(r"\d+\s*???", text)
        or re.fullmatch(r"\d+\s*???", text)
        or re.fullmatch(r"??(?:\s+\d{2}:\d{2})?", text)
        or re.fullmatch(r"??(?:\s+\d{2}:\d{2})?", text)
        or "?????" in text
        or "?????" in text
    )


def normalize_bilibili_date_line(line: str) -> str:
    text = clean_line(line)
    if "???" in text:
        text = text.split("???", 1)[0].strip()
    return normalize_relative_date(text) or normalize_yearless_date(text)


def extract_bilibili_published(raw_text: str) -> str:
    lines = [clean_line(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    for line in lines[:14]:
        if "?????" in line or "?????" in line:
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
    skip_titles = {"????", "?????", "?????", "??", "??", "??", "??", "??", "??"}

    for idx, line in enumerate(lines):
        if is_bilibili_date_line(line):
            current_published = normalize_bilibili_date_line(line)
            continue
        if not re.fullmatch(r"\d{2}:\d{2}", line):
            continue

        title = ""
        summary_parts: list[str] = []
        for j in range(idx + 1, min(idx + 10, len(lines))):
            candidate = lines[j]
            if is_bilibili_date_line(candidate):
                break
            if candidate in skip_titles:
                continue
            if re.fullmatch(r"[\d.]+[??]?", candidate):
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
        return result_error(source, "????? B ????")
    ensure_bilibili_context_ready(page.context)

    try:
        resolved_url, body_text, body_cards, cards, load_error = load_bilibili_page_candidates(
            page, target, limit=limit, timeout_ms=timeout_ms
        )
    except Exception as exc:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=target.page_url,
            ok=False,
            status=0,
            entries=[],
            error=format_bilibili_error(
                target=target,
                stage="fetch",
                kind=classify_bilibili_error(exc),
                url=target.page_url,
                attempt=1,
                detail=str(exc),
            ),
        )
    if not body_cards and not cards:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=resolved_url or target.page_url,
            ok=False,
            status=0,
            entries=[],
            error=load_error
            or format_bilibili_error(
                target=target,
                stage="parse",
                kind="empty",
                url=target.page_url,
                attempt=1,
                detail="?????????????",
            ),
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
        feed_url=resolved_url or target.page_url,
        ok=bool(entries),
        status=200 if entries else 0,
        entries=entries,
        error=""
        if entries
        else format_bilibili_error(
            target=target,
            stage="entries",
            kind="empty",
            url=resolved_url or target.page_url,
            attempt=1,
            detail="???????????????",
        ),
    )


def fetch_bilibili_dynamic(source: dict, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    return fetch_bilibili_dynamic_via_api(source, limit=limit, timeout_ms=timeout_ms)

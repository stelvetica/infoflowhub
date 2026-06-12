from __future__ import annotations

import ssl
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Iterable, List
from urllib.parse import urlparse

import feedparser

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import parse_published_datetime, resolve_web_target
from connectors._shared.web_fetch import fetch_web_many, fetch_web_source
from connectors.alphapai import fetch_alphapai_source


USER_AGENT = "infoflowhub-subscriptions/0.1"
MIN_SOURCE_ENTRIES = 10
MIN_SOURCE_DAYS = 3
FETCH_SOURCE_LIMIT = 60
RSS_RETRY_DELAYS = (0.0, 1.5, 4.0)


def _build_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )


def resolve_feed_url(source: dict, settings: dict | None = None) -> str:
    feed_url = source["feed_url"]
    provider = source.get("provider", "")
    fetch_via = source.get("fetch_via", "")
    rsshub = (settings or {}).get("rsshub", {})

    if provider != "rsshub":
        return feed_url

    parsed = urlparse(feed_url)
    suffix = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    if fetch_via == "rsshub-self-hosted":
        base = (rsshub.get("self_hosted_base") or "").rstrip("/")
        return f"{base}{suffix}" if base else feed_url

    if fetch_via == "rsshub-public":
        base = (rsshub.get("public_base") or "https://rsshub.app").rstrip("/")
        return f"{base}{suffix}"

    if rsshub.get("prefer_self_hosted") and rsshub.get("self_hosted_base"):
        base = rsshub["self_hosted_base"].rstrip("/")
        return f"{base}{suffix}"

    return feed_url


def fetch_feed(source_id: str, source_name: str, feed_url: str, timeout: int = 20, limit: int = FETCH_SOURCE_LIMIT) -> FeedFetchResult:
    context = ssl.create_default_context()
    last_error: Exception | None = None
    status = 0
    content = b""
    for delay in RSS_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(_build_request(feed_url), timeout=timeout, context=context) as response:
                status = getattr(response, "status", 200)
                content = response.read()
                last_error = None
                break
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        return FeedFetchResult(
            source_id=source_id,
            source_name=source_name,
            feed_url=feed_url,
            ok=False,
            status=0,
            entries=[],
            error=str(last_error),
        )

    parsed = feedparser.parse(content)
    error = str(getattr(parsed, "bozo_exception", "")) if getattr(parsed, "bozo", 0) else ""

    entries: List[FeedEntry] = []
    for item in parsed.entries[:limit]:
        entries.append(
            FeedEntry(
                source_id=source_id,
                source_name=source_name,
                title=(item.get("title") or "").strip(),
                link=(item.get("link") or "").strip(),
                published=(item.get("published") or item.get("updated") or item.get("created") or "").strip(),
                summary=(item.get("summary") or item.get("description") or "").strip(),
            )
        )

    entries = trim_entries(entries)

    return FeedFetchResult(
        source_id=source_id,
        source_name=source_name,
        feed_url=feed_url,
        ok=(status < 400),
        status=status,
        entries=entries,
        error=error,
    )


def should_fallback_to_web(source: dict, result: FeedFetchResult) -> bool:
    if source.get("provider") == "web":
        return False
    if result.ok and result.entries:
        return False
    target = resolve_web_target(source)
    return bool(target and target.site in {"youtube", "wechat"})


def fetch_many(sources: Iterable[dict], timeout: int = 20, settings: dict | None = None) -> List[FeedFetchResult]:
    source_list = list(sources)
    generic_web_sources = [source for source in source_list if source.get("provider") == "web" and source.get("id") != "alphapai"]
    web_results = {result.source_id: result for result in fetch_web_many(generic_web_sources, limit=FETCH_SOURCE_LIMIT)} if generic_web_sources else {}
    results: List[FeedFetchResult] = []
    for source in source_list:
        if source.get("id") == "alphapai":
            results.append(fetch_alphapai_source(source, limit=FETCH_SOURCE_LIMIT))
            continue
        if source.get("provider") == "web":
            results.append(web_results.get(source["id"]) or fetch_web_source(source))
            continue
        rss_result = fetch_feed(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=resolve_feed_url(source, settings=settings),
            timeout=timeout,
            limit=FETCH_SOURCE_LIMIT,
        )
        if should_fallback_to_web(source, rss_result):
            web_result = fetch_web_source(source)
            if web_result.ok and web_result.entries:
                results.append(web_result)
                continue
        results.append(rss_result)
    return results


def trim_entries(entries: List[FeedEntry]) -> List[FeedEntry]:
    if not entries:
        return entries

    sorted_entries = sorted(
        entries,
        key=lambda item: parse_published_datetime(item.published) or datetime.min,
        reverse=True,
    )
    threshold = datetime.now() - timedelta(days=MIN_SOURCE_DAYS)
    selected: List[FeedEntry] = []
    reached_min_count = False
    reached_min_days = False

    for item in sorted_entries:
        selected.append(item)
        published_dt = parse_published_datetime(item.published)
        if len(selected) >= MIN_SOURCE_ENTRIES:
            reached_min_count = True
        if published_dt and published_dt <= threshold:
            reached_min_days = True
        if reached_min_count and reached_min_days:
            break

    return selected if selected else sorted_entries

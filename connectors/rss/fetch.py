from __future__ import annotations

import urllib.request
from typing import Iterable, List
from urllib.parse import urlparse

import feedparser

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors.web.fetch import fetch_web_many, fetch_web_source


USER_AGENT = "infoflowhub-subscriptions/0.1"
DEFAULT_SOURCE_LIMIT = 20

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


def fetch_feed(source_id: str, source_name: str, feed_url: str, timeout: int = 20, limit: int = DEFAULT_SOURCE_LIMIT) -> FeedFetchResult:
    try:
        with urllib.request.urlopen(_build_request(feed_url), timeout=timeout) as response:
            status = getattr(response, "status", 200)
            content = response.read()
    except Exception as exc:
        return FeedFetchResult(
            source_id=source_id,
            source_name=source_name,
            feed_url=feed_url,
            ok=False,
            status=0,
            entries=[],
            error=str(exc),
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

    return FeedFetchResult(
        source_id=source_id,
        source_name=source_name,
        feed_url=feed_url,
        ok=(status < 400),
        status=status,
        entries=entries,
        error=error,
    )


def fetch_many(sources: Iterable[dict], timeout: int = 20, settings: dict | None = None) -> List[FeedFetchResult]:
    source_list = list(sources)
    web_sources = [source for source in source_list if source.get("provider") == "web"]
    web_results = {result.source_id: result for result in fetch_web_many(web_sources, limit=DEFAULT_SOURCE_LIMIT)} if web_sources else {}
    results: List[FeedFetchResult] = []
    for source in source_list:
        if source.get("provider") == "web":
            results.append(web_results.get(source["id"]) or fetch_web_source(source))
            continue
        results.append(
            fetch_feed(
                source_id=source["id"],
                source_name=source["name"],
                feed_url=resolve_feed_url(source, settings=settings),
                timeout=timeout,
                limit=DEFAULT_SOURCE_LIMIT,
            )
        )
    return results

from __future__ import annotations

from datetime import datetime

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import result_error
from connectors.wechat.api import fetch_wechat_articles, parse_wechat_publish_list
from connectors.wechat.auth import validate_wechat_auth_prerequisite


def _format_timestamp(value: int) -> str:
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y/%m/%d %H:%M")


def fetch_wechat_feed(source: dict, limit: int = 12) -> FeedFetchResult:
    auth_error = validate_wechat_auth_prerequisite(source)
    if auth_error:
        return result_error(source, auth_error)
    try:
        payload = fetch_wechat_articles(source, begin=0, count=max(limit, 10))
        articles, parse_error = parse_wechat_publish_list(payload)
        if parse_error:
            return result_error(source, parse_error)
        entries: list[FeedEntry] = []
        for article in articles[:limit]:
            title = str(article.get("title") or "").strip()
            link = str(article.get("link") or "").strip()
            if not title or not link:
                continue
            published = _format_timestamp(int(article.get("update_time") or article.get("create_time") or 0))
            summary = str(article.get("digest") or article.get("author") or "").strip()
            entries.append(
                FeedEntry(
                    source_id=source["id"],
                    source_name=source["name"],
                    title=title,
                    link=link,
                    published=published,
                    summary=summary,
                )
            )
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source["feed_url"],
            ok=True,
            status=200,
            entries=entries,
            error="",
        )
    except Exception as exc:
        return result_error(source, f"微信公众号抓取失败: {exc}")

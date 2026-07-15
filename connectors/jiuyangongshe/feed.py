from __future__ import annotations

import re
import ssl
import urllib.request
from typing import Any, List

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import USER_AGENT, clean_line, result_error


JYGS_HOME = "https://www.jiuyangongshe.com"
ARTICLE_LINK_BASE = f"{JYGS_HOME}/a/"

# 每页 15 条；首页即最新，配合 trim_entries 截断，无需翻页。
DEFAULT_LIMIT = 15
REQUEST_TIMEOUT = 20

# SSR HTML 文章结构：
#   <div class="fs13-ash">2026-07-14 16:00:48</div>  ← 日期（在标题前）
#   <div class="book-title ..."><span>标题</span></div>
#   <a href="/a/<id>"><span>摘要正文</span></a>
_ARTICLE_LINK_RE = re.compile(r'href="/a/([a-z0-9]+)"[^>]*><span>(.*?)</span>', re.DOTALL)
_DATE_RE = re.compile(r'class="fs13-ash"[^>]*>(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
_TITLE_RE = re.compile(r'<div class="book-title[^"]*"[^>]*><span>(.*?)</span></div>', re.DOTALL)
# 回溯窗口：单篇文章块（date+title+link）实测 < 1200 字符
_LOOKBACK = 1200


def _fetch_html(url: str) -> str:
    """纯 HTTP 抓取用户主页 HTML（未登录即可见完整文章列表）。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT, context=context) as response:
        return response.read().decode("utf-8", errors="ignore")


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def _parse_entries(html: str, limit: int) -> List[dict[str, str]]:
    """以 /a/<id> 链接为锚，向前回溯找最近的日期与标题，向后取摘要。"""
    entries: List[dict[str, str]] = []
    for match in _ARTICLE_LINK_RE.finditer(html):
        article_id = match.group(1)
        summary = clean_line(_strip_tags(match.group(2)))

        lookback = html[max(0, match.start() - _LOOKBACK):match.start()]
        date_matches = list(_DATE_RE.finditer(lookback))
        title_matches = list(_TITLE_RE.finditer(lookback))
        published = date_matches[-1].group(1) if date_matches else ""
        title = clean_line(_strip_tags(title_matches[-1].group(1))) if title_matches else ""

        # 边缘回退：无 book-title 的简短动态帖，用摘要前段作标题；
        # 摘要为空的置顶帖，用清理后的标题作摘要。
        if not title and summary:
            title = summary[:60]
        if not summary and title:
            summary = title

        if not article_id or not published:
            continue

        entries.append({
            "article_id": article_id,
            "title": title or f"韭研公社_{article_id[:8]}",
            "published": published,
            "summary": summary,
        })
        if len(entries) >= limit:
            break
    return entries


def fetch_jiuyangongshe_with_page(
    page: Any,
    source: dict,
    *,
    limit: int = DEFAULT_LIMIT,
    timeout_ms: int = 30000,
) -> FeedFetchResult:
    """纯 HTTP 抓取韭研公社用户主页文章列表。

    page 参数保留以兼容 web_fetch 调用约定，内部忽略（不起浏览器）。
    site_url 形如 https://www.jiuyangongshe.com/u/<uid>。
    """
    sid = source.get("id", "jiuyangongshe")
    sname = source.get("name", "韭研公社")
    feed_url = source.get("feed_url") or source.get("site_url") or ""

    try:
        html = _fetch_html(feed_url)
    except Exception as exc:
        return result_error(source, f"韭研公社主页抓取失败: {exc}")

    raw_entries = _parse_entries(html, limit)
    if not raw_entries:
        return FeedFetchResult(
            source_id=sid,
            source_name=sname,
            feed_url=feed_url,
            ok=False,
            status=0,
            entries=[],
            error="未解析到文章条目，页面结构可能已变更",
        )

    entries: List[FeedEntry] = [
        FeedEntry(
            source_id=sid,
            source_name=sname,
            title=item["title"],
            link=f"{ARTICLE_LINK_BASE}{item['article_id']}",
            published=item["published"],
            summary=item["summary"],
        )
        for item in raw_entries
    ]

    return FeedFetchResult(
        source_id=sid,
        source_name=sname,
        feed_url=feed_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )

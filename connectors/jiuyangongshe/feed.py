from __future__ import annotations

import html
import json
import re
import sqlite3
import ssl
import time
import urllib.request
from pathlib import Path
from typing import Any, List

from apps.subscriptions import rss_db
from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import USER_AGENT, clean_line, result_error
from connectors._shared.md_store import write_markdown_file


JYGS_HOME = "https://www.jiuyangongshe.com"
ARTICLE_LINK_BASE = f"{JYGS_HOME}/a/"
ARTICLE_BASE = ARTICLE_LINK_BASE
MARKDOWN_DIR = Path(__file__).resolve().parents[2] / "data" / "jiuyangongshe_markdown"

# 每页 15 条；首页即最新，配合 trim_entries 截断，无需翻页。
DEFAULT_LIMIT = 15
REQUEST_TIMEOUT = 20
# 正文逐条串行抓取，间隔 0.4s 限速（单条超时即 REQUEST_TIMEOUT=20s）
BODY_FETCH_INTERVAL = 0.4

# SSR HTML 文章结构：
#   <div class="fs13-ash">2026-07-14 16:00:48</div>  ← 日期（在标题前）
#   <div class="book-title ..."><span>标题</span></div>
#   <a href="/a/<id>"><span>摘要正文</span></a>
_ARTICLE_LINK_RE = re.compile(r'href="/a/([a-z0-9]+)"[^>]*><span>(.*?)</span>', re.DOTALL)
_DATE_RE = re.compile(r'class="fs13-ash"[^>]*>(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
_TITLE_RE = re.compile(r'<div class="book-title[^"]*"[^>]*><span>(.*?)</span></div>', re.DOTALL)
# 回溯窗口：单篇文章块（date+title+link）实测 < 1200 字符
_LOOKBACK = 1200

# 正文页：`.text-box.fsDetail` 容器在纯 HTTP 响应里是空 div，正文在 Nuxt payload 的
# content:"…" JS 转义字符串里（内含 \\" 与 &quot;，非贪婪正则必被截断）。
# payload 首个 article_id 即当前文章对象，其后出现的 article_id/content 属推荐位与评论对象。
PAYLOAD_ANCHOR = "article_id:"
CONTENT_KEY = 'content:"'
TITLE_KEY = 'title:"'
PAYLOAD_FIELD_WINDOW = 2000
_DETAIL_TITLE_RE = re.compile(r'<div class="fs28-bold[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_DETAIL_DATE_RE = re.compile(r'<div class="date fs14"[^>]*>(.*?)</div>', re.DOTALL)
DISCLAIMER_PREFIXES = ("作者利益披露", "声明：", "声明:", "免责声明")
_BLOCK_TAG_RE = re.compile(
    r"<br\s*/?>|</(?:p|div|h[1-6]|li|tr|td|section|blockquote|ul|ol)\s*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
# 正文里的超链接（公社内容精选就是一篇「带链接的文章清单」，链接是它的核心信息）
_ANCHOR_RE = re.compile(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_ARTICLE_ID_IN_LINK_RE = re.compile(r"/a/([a-z0-9]+)")
_PUBLISHED_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")


def _fetch_html(url: str) -> str:
    """纯 HTTP 抓取页面 HTML（用户主页与正文页均未登录可见）。"""
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


def _parse_entries(html_text: str, limit: int) -> List[dict[str, str]]:
    """以 /a/<id> 链接为锚，向前回溯找最近的日期与标题，向后取摘要。"""
    entries: List[dict[str, str]] = []
    for match in _ARTICLE_LINK_RE.finditer(html_text):
        article_id = match.group(1)
        summary = clean_line(_strip_tags(match.group(2)))

        lookback = html_text[max(0, match.start() - _LOOKBACK):match.start()]
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


def _scan_js_string(text: str, start: int) -> tuple[str, int]:
    """从 start 起扫描 JS 字符串内容，直到未转义的双引号。

    返回 (原始内容, 收尾引号下标)；未找到收尾引号（结构异常）时返回 ("", start)。
    不能用非贪婪正则：正文含 \\" 转义，正则会在第一个 \\" 处提前收尾、截断正文。
    """
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2  # 跳过转义对（\\" 与 \\\\ ）
            continue
        if char == '"':
            return text[start:index], index
        index += 1
    return "", start


def _decode_js_string(raw: str) -> str:
    """解 JS 字符串转义（\\u003C / \\" 等），失败按空处理。"""
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return ""


def _payload_string(html_text: str, key: str) -> str:
    """取 payload 中当前文章对象的字符串字段值（未做 HTML 实体还原）。

    key 取 CONTENT_KEY / TITLE_KEY 那种字面量（字段名 + 冒号 + 引号）。
    只在首个 article_id 锚点之后的窗口内找字段；字段值是变量引用时（如 content:b，
    表示该字段为空）返回空串——避免误取推荐位/评论对象里同名字段的内容。
    """
    anchor = html_text.find(PAYLOAD_ANCHOR)
    start = anchor if anchor >= 0 else 0
    # 用编译后的 pattern.search(text, pos)：re.search 的第三个位置参数是 flags，不是起点
    match = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(key.rstrip('"'))).search(html_text, start)
    if not match or match.start() - start > PAYLOAD_FIELD_WINDOW:
        return ""
    quote_index = match.end()
    if html_text[quote_index:quote_index + 1] != '"':
        return ""
    raw, _ = _scan_js_string(html_text, quote_index + 1)
    return _decode_js_string(raw)


def _anchor_to_markdown(match: re.Match[str]) -> str:
    """`<a href="…">标题</a>` → `[标题](url)`；非 http(s) 链接只保留标题文本。

    公社内容精选本身就是「带链接的文章清单」，链接是它的核心信息，去标签时不能一起丢掉。
    """
    href = html.unescape(match.group(1)).strip()
    label = clean_line(html.unescape(_TAG_RE.sub("", match.group(2))))
    if not label:
        return ""
    if not href.lower().startswith(("http://", "https://")):
        return label
    return f"[{label}]({href})"


def _extract_article_body(html_text: str) -> str:
    """提取正文文本：payload content → 链接转 markdown → 去标签（块级标签转换行）→ 逐行清理 → 去免责段。

    免责段（作者利益披露 / 声明）在真实页面里位于 payload 之外的独立 SSR 节点，不会进
    正文；此处再按行首前缀兜底丢弃它及其后全部内容。
    """
    fragment = _payload_string(html_text, CONTENT_KEY)
    if not fragment:
        return ""
    # 先把 <a> 转成 markdown 链接，再做去标签——否则链接地址会随标签一起被剥掉
    fragment = _ANCHOR_RE.sub(_anchor_to_markdown, fragment)
    text = _BLOCK_TAG_RE.sub("\n", fragment)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines: List[str] = []
    previous = ""
    for raw_line in text.splitlines():
        line = clean_line(raw_line)
        if not line or line == previous:
            continue
        if line.startswith(DISCLAIMER_PREFIXES):
            break
        lines.append(line)
        previous = line
    return "\n\n".join(lines)


def _extract_article_meta(html_text: str, article_id: str, fallback_published: str) -> tuple[str, str]:
    """提取正文页标题与发布时间。

    标题：`.fs28-bold` → payload title → 韭研公社_{id[:8]}。
    发布时间：`.date fs14` 前 19 字符（忽略尾部地域，如 "2024-11-29 20:55:27 河北省"）
    → 列表页给的 published。
    """
    title = ""
    match = _DETAIL_TITLE_RE.search(html_text)
    if match:
        title = clean_line(_strip_tags(html.unescape(match.group(1))))
    if not title:
        title = clean_line(_strip_tags(html.unescape(_payload_string(html_text, TITLE_KEY))))
    if not title:
        title = f"韭研公社_{article_id[:8]}"

    published = ""
    match = _DETAIL_DATE_RE.search(html_text)
    if match:
        published = clean_line(_strip_tags(html.unescape(match.group(1))))[:19]
    return title, published or fallback_published


def _source_short_name(source: dict) -> str:
    """源名去掉 "资讯 " 前缀（韭菜团子 / 韭韭八十一）。"""
    name = clean_line(str(source.get("name") or ""))
    return name.removeprefix("资讯 ").strip() or "韭研公社"


def _format_published(value: str) -> str:
    """YYYY-MM-DD HH:MM(:SS) → YYYY/MM/DD HH:MM（与蓝宝书 markdown 头一致）。"""
    match = _PUBLISHED_RE.match(clean_line(value))
    if not match:
        return clean_line(value)
    return f"{match.group(1)}/{match.group(2)}/{match.group(3)} {match.group(4)}:{match.group(5)}"


def _get_existing_article_ids(source_id: str) -> set[str]:
    """已入库条目的 article_id 集合。

    用 article_id 而非标题去重：韭研公社标题会重复，article_id 才是稳定键。
    """
    if not rss_db.DB_PATH.exists():
        return set()
    conn = sqlite3.connect(str(rss_db.DB_PATH))
    try:
        rows = conn.execute("SELECT link FROM rss_entries WHERE source_id = ?", (source_id,)).fetchall()
    except Exception:
        return set()
    finally:
        conn.close()
    article_ids: set[str] = set()
    for (link,) in rows:
        match = _ARTICLE_ID_IN_LINK_RE.search(str(link or ""))
        if match:
            article_ids.add(match.group(1))
    return article_ids


def _write_article_markdown(source: dict, article_id: str, title: str, published: str, body: str) -> str:
    """正文落盘为 markdown，返回绝对路径（文件名含源名与 article_id 摘要）。"""
    source_short = _source_short_name(source)
    return write_markdown_file(
        MARKDOWN_DIR,
        title=title,
        published=_format_published(published),
        source_line=f"韭研公社 {source_short}",
        page_url=f"{ARTICLE_BASE}{article_id}",
        body_text=body,
        extra_key=article_id,
        filename_prefix=f"{source_short}_",
    )


def _fetch_article_markdown(source: dict, item: dict[str, str]) -> str:
    """抓单条正文页并落盘，返回 markdown 绝对路径。

    单条失败只降级本条（不中断整源）：正文取不到就回退列表摘要，皆空则返回空串
    （条目照常入库，markdown_path 留空）。
    """
    article_id = item["article_id"]
    title, published, body = item["title"], item["published"], ""
    try:
        article_html = _fetch_html(f"{ARTICLE_BASE}{article_id}")
    except Exception:
        article_html = ""
    if article_html:
        title, published = _extract_article_meta(article_html, article_id, item["published"])
        body = _extract_article_body(article_html)
    if not body:
        body = item["summary"]
    if not body:
        return ""
    try:
        return _write_article_markdown(source, article_id, title, published, body)
    except Exception:
        return ""


def fetch_jiuyangongshe_with_page(
    page: Any,
    source: dict,
    *,
    limit: int = DEFAULT_LIMIT,
    timeout_ms: int = 30000,
    with_body: bool = True,
) -> FeedFetchResult:
    """纯 HTTP 抓取韭研公社用户主页文章列表，并逐条抓正文落盘 markdown。

    page 参数保留以兼容 web_fetch 调用约定，内部忽略（不起浏览器）。
    site_url 形如 https://www.jiuyangongshe.com/u/<uid>。
    只对未入库（按 article_id 判断）的条目抓正文；with_body=False 时纯列表。

    调用方（connectors.rss.fetch）之后还会按时间 trim 条目，被裁掉的条目已落盘但未入库，
    下轮会重复抓一次（每源每轮最多 1 条置顶旧帖，量级可接受）。
    """
    sid = source.get("id", "jiuyangongshe")
    sname = source.get("name", "韭研公社")
    feed_url = source.get("feed_url") or source.get("site_url") or ""

    try:
        html_text = _fetch_html(feed_url)
    except Exception as exc:
        return result_error(source, f"韭研公社主页抓取失败: {exc}")

    raw_entries = _parse_entries(html_text, limit)
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

    existing_ids = _get_existing_article_ids(sid) if with_body else set()
    entries: List[FeedEntry] = []
    fetched = 0
    for item in raw_entries:
        markdown_path = ""
        if with_body and item["article_id"] not in existing_ids:
            if fetched:
                time.sleep(BODY_FETCH_INTERVAL)
            fetched += 1
            markdown_path = _fetch_article_markdown(source, item)
        entries.append(
            FeedEntry(
                source_id=sid,
                source_name=sname,
                title=item["title"],
                link=f"{ARTICLE_LINK_BASE}{item['article_id']}",
                published=item["published"],
                summary=item["summary"],
                markdown_path=markdown_path,
            )
        )

    return FeedFetchResult(
        source_id=sid,
        source_name=sname,
        feed_url=feed_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )

"""Alpha派社媒（公众号聚合）默认页解析与抓取。

只读默认打开状态的 DOM：不点页签、不点任何行业/内容/时间筛选，也不调页面内裸 REST
（页面内裸 fetch 返回 401，需 App 自带鉴权头）。因此这里只吃 HTML 文本。

默认界面结构（真实 HTML 校准，2026-09-22）：
    .list-item-card-container
      .card-header .source-info .source-name   -> 作者（= 公众号名）
      .card-body .content-wrapper
        .title-wrapper .title                   -> 标题
        .tag.tag-original                       -> 「原创」标记（可选）
        .summary .source-paipai + 兄弟 span      -> @PaiPai AI 摘要（可多行）
        .footer-info .category / .category.industry-name / .date
                                                 -> 内容分类 / 行业标签(多个) / 相对时间
卡片本身无独立链接，link 统一回落社媒默认页。
"""

from __future__ import annotations

import re
import sqlite3
import time as _time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import List

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import clean_line, normalize_relative_date, normalize_title_key, now_text
from connectors.alphapai.feed import looks_like_login_page


SOCIAL_URL = "https://alphapai-web.rabyte.cn/reading/social-media/index"
CARD_CLASS = "list-item-card-container"
LIST_SELECTORS = (".list-wrapper", ".scroll-box")
SOCIAL_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "subscriptions.sqlite3"

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_PAIPAI_PREFIX_RE = re.compile(r"^@PaiPai\s*[:：]\s*")
_ABS_DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$")
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})[-/月](\d{1,2})日?(?:\s+(\d{1,2}):(\d{2}))?$")
_JUST_NOW_LABELS = frozenset({"刚刚", "刚才"})


# ---- 极简 DOM 树（stdlib，不引第三方解析依赖） ----


class _Node:
    __slots__ = ("tag", "classes", "children", "texts")

    def __init__(self, tag: str, classes: frozenset[str]) -> None:
        self.tag = tag
        self.classes = classes
        self.children: list["_Node"] = []
        self.texts: list[str] = []


def _classes_of(attrs: list[tuple[str, str | None]]) -> frozenset[str]:
    for name, value in attrs:
        if name == "class":
            return frozenset((value or "").split())
    return frozenset()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", frozenset())
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, _classes_of(attrs))
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_Node(tag, _classes_of(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].texts.append(data)


def _iter_by_class(node: _Node, class_name: str, *, skip_nested: bool = False):
    for child in node.children:
        if class_name in child.classes:
            yield child
            if skip_nested:
                # 真实 DOM 会出现同名嵌套容器（外层卡片内再套一层同名容器）；
                # 命中的容器内部不再下钻，避免同一张卡被解析成两条。
                continue
        yield from _iter_by_class(child, class_name, skip_nested=skip_nested)


def _first_by_class(node: _Node, class_name: str) -> _Node | None:
    return next(_iter_by_class(node, class_name), None)


def _node_text(node: _Node | None) -> str:
    if node is None:
        return ""
    parts = list(node.texts)
    for child in node.children:
        if child.tag in ("script", "style"):
            continue
        parts.append(_node_text(child))
    return clean_line("".join(parts))


# ---- 字段解析 ----


def _parse_time_label(label: str) -> str:
    """相对/绝对时间 -> YYYY/MM/DD HH:MM（与 feed.py `_parse_time_label` 同格式）。"""
    line = clean_line(label)
    if not line or line in _JUST_NOW_LABELS:
        return now_text()

    relative = normalize_relative_date(line)
    if relative:
        return relative

    match = _ABS_DATE_RE.match(line)
    if match:
        year, month, day, hour, minute = match.groups()
        return f"{int(year):04d}/{int(month):02d}/{int(day):02d} {int(hour or 0):02d}:{int(minute or 0):02d}"

    match = _MONTH_DAY_RE.match(line)
    if match:
        month, day, hour, minute = match.groups()
        now = datetime.now()
        # 无年份的「月日」补当年即可，但若补出的时间晚于当前时刻（如 9 月遇到「12月30日」），
        # 说明它只能是去年的，年份减一——否则排序会把未来时间排到最前。
        stamp = (int(month), int(day), int(hour or 0), int(minute or 0))
        year = now.year - 1 if stamp > (now.month, now.day, now.hour, now.minute) else now.year
        return f"{year:04d}/{stamp[0]:02d}/{stamp[1]:02d} {stamp[2]:02d}:{stamp[3]:02d}"

    return now_text()


def parse_social_cards(html_text: str) -> List[dict]:
    """从社媒默认页 HTML 中解析卡片。只吃 HTML 文本，不依赖 token / REST。"""
    builder = _TreeBuilder()
    builder.feed(html_text or "")
    builder.close()

    cards: List[dict] = []
    for card in _iter_by_class(builder.root, CARD_CLASS, skip_nested=True):
        title = _node_text(_first_by_class(card, "title"))
        if not title:
            continue

        category = ""
        industries: List[str] = []
        footer = _first_by_class(card, "footer-info")
        if footer is not None:
            for item in _iter_by_class(footer, "category"):
                if "industry-name" in item.classes:
                    text = _node_text(item)
                    if text:
                        industries.append(text)
                elif not category:
                    category = _node_text(item)

        time_label = _node_text(_first_by_class(card, "date"))
        cards.append(
            {
                "author": _node_text(_first_by_class(card, "source-name")),
                "title": title,
                "original": _first_by_class(card, "tag-original") is not None,
                "summary": _node_text(_first_by_class(card, "summary")),
                "category": category,
                "industries": industries,
                "time_label": time_label,
                "published": _parse_time_label(time_label),
            }
        )
    return cards


def entry_title(card: dict) -> str:
    """作者稳定写入标题：FeedEntry 无作者字段，而 save_entries 按标题去重，
    标题不带作者会让不同公众号的同名文章互相顶掉（丢条目）。"""
    author = clean_line(card.get("author", ""))
    title = clean_line(card.get("title", ""))
    return f"{author}｜{title}" if author else title


def entry_summary(card: dict) -> str:
    author = clean_line(card.get("author", ""))
    summary = _PAIPAI_PREFIX_RE.sub("", clean_line(card.get("summary", "")))
    category = clean_line(card.get("category", ""))
    industries = [item for item in (clean_line(x) for x in card.get("industries", [])) if item]

    lines: List[str] = []
    if author:
        lines.append(f"作者：{author}")
    if summary:
        lines.append(f"@PaiPai 摘要：{summary}")
    if category:
        lines.append(f"内容分类：{category}")
    if industries:
        lines.append("行业标签：" + " / ".join(industries))
    return "\n".join(lines)


def build_entries(source: dict, cards: List[dict], *, limit: int = 0) -> List[FeedEntry]:
    sid = source.get("id", "alphapai-social-media")
    sname = source.get("name", "资讯 Alpha派公众号")
    link = source.get("feed_url") or SOCIAL_URL
    entries: List[FeedEntry] = []
    for card in cards[: limit or None]:
        title = entry_title(card)
        if not title:
            continue
        entries.append(
            FeedEntry(
                source_id=sid,
                source_name=sname,
                title=title,
                link=link,
                published=card.get("published") or now_text(),
                summary=entry_summary(card),
            )
        )
    return entries


def _get_existing_keys(source_id: str) -> set[str]:
    """已入库标题的归一化键集合（与 save_entries 的标题去重口径一致）。"""
    if not SOCIAL_DB_PATH.exists():
        return set()
    try:
        conn = sqlite3.connect(str(SOCIAL_DB_PATH))
        rows = conn.execute("SELECT title FROM rss_entries WHERE source_id = ?", (source_id,)).fetchall()
        conn.close()
    except Exception:
        return set()
    return {normalize_title_key(str(title or "")) for (title,) in rows if title}


# ---- 抓取 ----


def _list_html(page) -> str:
    for selector in LIST_SELECTORS:
        try:
            html = page.locator(selector).first.inner_html()
        except Exception:
            html = ""
        if CARD_CLASS in (html or ""):
            return html
    return ""


def _wait_list_html(page, timeout_ms: int) -> str:
    deadline = _time.time() + max(timeout_ms, 0) / 1000
    while True:
        html = _list_html(page)
        if html:
            return html
        if _time.time() >= deadline:
            return ""
        _time.sleep(0.5)


def fetch_alphapai_social_with_page(page, source: dict, timeout_ms: int = 120000, *, limit: int = 12) -> FeedFetchResult:
    sid = source.get("id", "alphapai-social-media")
    sname = source.get("name", "资讯 Alpha派公众号")
    feed_url = source.get("feed_url") or SOCIAL_URL

    def fail(status: int, error: str) -> FeedFetchResult:
        return FeedFetchResult(sid, sname, feed_url, False, status, [], error)

    if "/reading/social-media" not in str(page.url or "").strip():
        try:
            page.goto(SOCIAL_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            return fail(0, f"页面加载失败: {exc}")

    if looks_like_login_page(page):
        return fail(401, "社媒页面登录态失效")

    html = _wait_list_html(page, timeout_ms=min(timeout_ms, 20000))
    if not html:
        if looks_like_login_page(page):
            return fail(401, "社媒页面登录态失效")
        return fail(0, "社媒页面未完成加载")

    cards = parse_social_cards(html)
    if not cards:
        if looks_like_login_page(page):
            return fail(401, "社媒页面登录态失效")
        return fail(200, "页面可访问，但未解析到文章卡片")

    existing = _get_existing_keys(sid)
    fresh = [card for card in cards if normalize_title_key(entry_title(card)) not in existing]
    entries = build_entries(source, fresh, limit=limit)

    return FeedFetchResult(
        source_id=sid,
        source_name=sname,
        feed_url=feed_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )
"""Alpha派社媒（公众号聚合）默认页：解析器 + 接线 + 去重。

全部用真实抓取的 fixture 与假页面，不连真实 daemon。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from apps.subscriptions import rss_db
from connectors._shared import web_fetch
from connectors._shared.common import resolve_web_target
from connectors.alphapai import social


FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_HTML = (FIXTURES / "alphapai_social_media_default.html").read_text(encoding="utf-8")
EDGE_HTML = (FIXTURES / "alphapai_social_media_edge.html").read_text(encoding="utf-8")
PUBLISHED_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}$")

SOCIAL_URL = "https://alphapai-web.rabyte.cn/reading/social-media/index"
SOURCE = {
    "id": "alphapai-social-media",
    "name": "资讯 Alpha派公众号",
    "feed_url": SOCIAL_URL,
    "site_url": SOCIAL_URL,
    "provider": "web",
    "fetch_via": "web",
    "kind": "web",
    "enabled": True,
    "note": "",
    "channel": "alphapai",
    "auth_key": "alphapai_main",
    "fallback_mode": "none",
}


class FakeLocator:
    def __init__(self, html: str) -> None:
        self._html = html

    @property
    def first(self) -> "FakeLocator":
        return self

    def inner_html(self) -> str:
        return self._html

    def inner_text(self, timeout: float | None = None) -> str:
        return ""


class FakePage:
    def __init__(self, html: str, url: str = SOCIAL_URL) -> None:
        self._html = html
        self.url = url
        self.closed = False

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._html)

    def goto(self, url: str, wait_until: str | None = None, timeout: float | None = None) -> None:
        self.url = url

    def close(self) -> None:
        self.closed = True


# ---- 解析器：真实 fixture ----


def test_real_fixture_card_count_and_titles():
    cards = social.parse_social_cards(REAL_HTML)
    assert len(cards) == 7
    assert all(card["title"] for card in cards)


def test_author_and_title_do_not_cross_cards():
    cards = social.parse_social_cards(REAL_HTML)
    by_author: dict[str, list[str]] = {}
    for card in cards:
        by_author.setdefault(card["author"], []).append(card["title"])

    assert by_author["贝叶斯之美"] == ["从真实任务看Meta Muse的能力边界在哪？"]
    assert by_author["finn的投研记录"] == ["巴克莱-现房销售与房贷期限延长至40年能否提振楼市？"]
    assert by_author["中金点睛"] == [
        "中金：海外AI投资变化影响多大",
        "中金：商业医疗险条款规范化，供给提质以促需",
    ]
    # 水木纪要的三张卡片里不得混入别人的标题
    assert all("Meta Muse" not in title and "商业医疗险" not in title for title in by_author["水木纪要"])
    assert len(by_author["水木纪要"]) == 3


def test_paipai_summary_is_not_truncated_by_category_or_tags():
    card = social.parse_social_cards(REAL_HTML)[0]
    assert card["summary"].startswith("@PaiPai：")
    # 末句（分类/标签之后的内容）仍在
    assert "个人AI操作系统" in card["summary"]
    assert "尖峰型智能" in card["summary"]
    # 分类与标签不落进摘要字段
    assert "产业分析" not in card["summary"]
    assert "AI应用" not in card["summary"]


def test_category_and_industry_tags_split():
    cards = social.parse_social_cards(REAL_HTML)
    assert cards[0]["category"] == "产业分析"
    assert cards[0]["industries"] == ["AI应用", "计算机"]
    assert cards[3]["category"] == "行业新闻及点评"
    assert cards[3]["industries"] == ["非银金融", "医药生物"]


def test_original_flag_and_time_label():
    cards = social.parse_social_cards(REAL_HTML)
    assert cards[0]["original"] is True
    assert cards[3]["original"] is False
    assert cards[0]["time_label"] == "2小时前"


def test_relative_time_becomes_sortable_absolute_time():
    now = datetime.now()
    published = social._parse_time_label("8小时前")
    assert PUBLISHED_RE.match(published)
    delta = now - datetime.strptime(published, "%Y/%m/%d %H:%M")
    assert timedelta(hours=7, minutes=59) <= delta <= timedelta(hours=8, minutes=1)

    yesterday = datetime.strptime(social._parse_time_label("昨天"), "%Y/%m/%d %H:%M")
    assert (now.date() - yesterday.date()).days == 1


@pytest.mark.parametrize(
    "label",
    ["3小时前", "45分钟前", "昨天", "前天", "刚刚", "2026-09-21 10:30", "09-21 10:30", "10:30"],
)
def test_time_labels_are_always_absolute(label):
    assert PUBLISHED_RE.match(social._parse_time_label(label))


def test_repeated_parse_is_stable():
    assert social.parse_social_cards(REAL_HTML) == social.parse_social_cards(REAL_HTML)


def test_nested_same_class_container_does_not_duplicate_card():
    """真实 DOM 会在卡片内再套一层同名容器；只认最外层，避免同一张卡出两条。"""
    html = """
    <div class="list-wrapper">
      <div class="list-item-card-container">
        <div class="title">外层标题</div>
        <div class="list-item-card-container">
          <div class="title">内层标题</div>
        </div>
      </div>
    </div>
    """
    cards = social.parse_social_cards(html)
    assert len(cards) == 1
    assert cards[0]["title"] == "外层标题"


# ---- 时间补年不得产生未来 ----


def test_month_day_without_year_never_yields_future_time():
    now = datetime.now()
    future = now + timedelta(days=40)
    parsed = datetime.strptime(
        social._parse_time_label(f"{future.month}月{future.day}日"), "%Y/%m/%d %H:%M"
    )
    assert parsed <= now


def test_month_day_without_year_earlier_day_stays_current_year():
    now = datetime.now()
    if now.day <= 1:
        pytest.skip("当月 1 日没有同月更早的日期可用")
    parsed = datetime.strptime(
        social._parse_time_label(f"{now.month}月{now.day - 1}日"), "%Y/%m/%d %H:%M"
    )
    assert parsed.year == now.year and parsed < now


# ---- 解析器：边界 fixture ----


def test_edge_cards_skip_empty_title_and_keep_missing_fields_safe():
    cards = social.parse_social_cards(EDGE_HTML)
    assert [card["title"] for card in cards] == [
        "多行摘要卡片标题",
        "缺作者与标签的卡片",
        "无摘要的行业新闻卡片",
    ]
    assert cards[1]["author"] == ""
    assert cards[1]["industries"] == []
    assert cards[1]["time_label"] == ""
    assert cards[2]["summary"] == ""
    assert cards[2]["original"] is False


def test_edge_multi_line_summary_survives():
    card = social.parse_social_cards(EDGE_HTML)[0]
    assert "第一行要点" in card["summary"] and "第二行要点" in card["summary"] and "第三行要点" in card["summary"]


def test_edge_entries_use_source_link_and_structured_summary():
    entries = social.build_entries(SOURCE, social.parse_social_cards(EDGE_HTML))
    assert [entry.title for entry in entries] == [
        "贝叶斯之美｜多行摘要卡片标题",
        "缺作者与标签的卡片",
        "中金点睛｜无摘要的行业新闻卡片",
    ]
    assert all(entry.link == SOCIAL_URL for entry in entries)
    assert "作者：贝叶斯之美" in entries[0].summary
    assert "行业标签：AI应用 / 计算机" in entries[0].summary
    # 缺作者时不写作者行，也不给标题加前缀
    assert "作者：" not in entries[1].summary


def test_build_entries_respects_limit():
    cards = social.parse_social_cards(REAL_HTML)
    assert len(social.build_entries(SOURCE, cards, limit=3)) == 3


# ---- 接线 ----


def test_social_url_resolves_to_dedicated_site():
    target = resolve_web_target(SOURCE)
    assert target is not None
    assert target.site == "alphapai_social"
    assert target.page_url == SOCIAL_URL


def test_bluebook_url_still_resolves_to_alphapai():
    target = resolve_web_target({"site_url": "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"})
    assert target is not None and target.site == "alphapai"


def test_social_source_uses_shared_runner_and_dispatches_to_social_fetcher(monkeypatch):
    assert web_fetch._needs_shared_runner(SOURCE) is True

    seen: list = []
    monkeypatch.setattr(
        web_fetch,
        "fetch_alphapai_social_with_page",
        lambda page, source, timeout_ms=0, limit=0: seen.append(page) or social.fetch_alphapai_social_with_page(page, source, limit=limit),
    )

    class FakeSession:
        def acquire_page_by_url(self, url, **kwargs):
            return FakePage(REAL_HTML)

    result = web_fetch._fetch_one_via_session(FakeSession(), SOURCE, limit=60, timeout_ms=1000)

    assert seen and result.ok and len(result.entries) == 7


# ---- 去重 ----


def _fetch(page_html: str, limit: int = 60):
    return social.fetch_alphapai_social_with_page(FakePage(page_html), SOURCE, limit=limit)


def test_repeated_fetch_does_not_duplicate_entries(monkeypatch, tmp_path):
    db_path = tmp_path / "subscriptions.sqlite3"
    monkeypatch.setattr(social, "SOCIAL_DB_PATH", db_path)
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", db_path)

    first = _fetch(REAL_HTML)
    assert first.ok and len(first.entries) == 7
    assert rss_db.save_entries(first.entries) == 7

    second = _fetch(REAL_HTML)
    assert second.ok and second.entries == []
    assert rss_db.save_entries(second.entries) == 0

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT COUNT(*) FROM rss_entries WHERE source_id = ?", (SOURCE["id"],)
    ).fetchone()
    conn.close()
    assert rows[0] == 7


def test_login_page_reports_401(monkeypatch):
    monkeypatch.setattr(social, "looks_like_login_page", lambda page: True)
    result = _fetch(REAL_HTML)
    assert not result.ok and result.status == 401
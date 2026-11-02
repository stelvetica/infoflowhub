"""抖音（破壁狗 / 马儿聊世界）迁到 bsk 后端：探测式响应拦截、纯 DOM 取数、分派与时间降级。

假 page / 假会话打桩，不连真实 daemon 与 Chrome。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.subscriptions import rss_config
from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared import web_fetch
from connectors._shared.bsk_session import BskRunnerSession
from connectors.douyin.feed import (
    _navigate_and_collect_payload_maps,
    _normalize_douyin_published,
    _supports_response_listener,
    fetch_douyin_subscription_with_page,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ID = "破壁狗"
SOURCE = {
    "id": SRC_ID,
    "name": "观点 破壁狗",
    "feed_url": "https://www.douyin.com/user/MS4wLjABAAAA_Vbo0sbxI4rBJbHSupB1niZe0B9Sh9mE3vbD-utRgIW669BslyZhVVEKyFanjyfo",
    "site_url": "https://www.douyin.com/user/MS4wLjABAAAA_Vbo0sbxI4rBJbHSupB1niZe0B9Sh9mE3vbD-utRgIW669BslyZhVVEKyFanjyfo",
    "auth_key": "douyin_shared",
    "provider": "web",
    "fetch_via": "web",
}

# bsk 真实 DOM 里卡片没有 time/publish/date 元素（实测 time_els=[]），
# 故 evaluate 返回的 published 恒为空串，时间由 now_text() 兜底。
DOM_CARDS = [
    {
        "video_id": "7687074567461915914",
        "href": "https://www.douyin.com/video/7687074567461915914",
        "title": "破壁狗：AI叛乱下：AI送给人类的文明寓言",
        "published": "",
        "raw_text": "921 破壁狗：AI叛乱下：AI送给人类的文明寓言",
    },
    {
        "video_id": "7687074629990599976",
        "href": "https://www.douyin.com/video/7687074629990599976",
        "title": "破壁狗：AI叛乱中：智能体的牺牲和遗言？",
        "published": "",
        "raw_text": "436 破壁狗：AI叛乱中：智能体的牺牲和遗言？",
    },
]


class _FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self._page = page
        self.selector = selector
        self.first = self

    def wait_for(self, timeout=None, state="visible") -> None:
        self._page.waited.append((self.selector, timeout))

    def inner_text(self, timeout=None) -> str:
        return self._page.body_text


class FakeBskPage:
    """bsk 形态：无 on / remove_listener（响应拦截不可用），其余调用面齐全。"""

    def __init__(self, cards=None, body_text="") -> None:
        self.cards = DOM_CARDS if cards is None else cards
        self.body_text = body_text
        self.goto_url = ""
        self.waited: list = []
        self.evaluated: list = []

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        return None

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluated.append((js, arg, kwargs))
        return self.cards


class FakeCopyPage(FakeBskPage):
    """copy 形态：真 Playwright page 有 on / remove_listener。"""

    def __init__(self, payloads=None) -> None:
        super().__init__()
        self.payloads = payloads or []
        self.listeners: list = []
        self.removed: list = []

    def on(self, event, handler) -> None:
        self.listeners.append((event, handler))

    def remove_listener(self, event, handler) -> None:
        self.removed.append((event, handler))


def _response(url, payload):
    return SimpleNamespace(url=url, json=lambda: payload)


# ---- 探测式响应拦截 ----


def test_supports_response_listener_detects_backend():
    assert _supports_response_listener(FakeCopyPage()) is True
    assert _supports_response_listener(FakeBskPage()) is False


def test_bsk_page_navigates_without_listener():
    page = FakeBskPage()

    publish_map, title_map, summary_map, error = _navigate_and_collect_payload_maps(
        page, SOURCE["site_url"], 60000
    )

    # 只导航、不挂监听：三个 map 全空，无错误
    assert page.goto_url == SOURCE["site_url"]
    assert publish_map == {} and title_map == {} and summary_map == {}
    assert error == ""


def test_copy_page_still_intercepts_and_maps_payload():
    page = FakeCopyPage()
    payload = {
        "aweme_list": [
            {"aweme_id": "7687074567461915914", "create_time": 1789830900, "desc": "标题A"},
        ]
    }

    # 挂监听后由 _navigate_and_collect_payload_maps 内部的 on_response 处理：
    # 这里模拟 page.on 被调用时立即触发一次响应。
    def fake_on(event, handler):
        page.listeners.append((event, handler))
        handler(_response("https://www.douyin.com/aweme/v1/web/aweme/post/?x=1", payload))

    page.on = fake_on

    publish_map, title_map, summary_map, error = _navigate_and_collect_payload_maps(
        page, SOURCE["site_url"], 60000
    )

    assert page.listeners and page.listeners[0][0] == "response"
    assert page.removed and page.removed[0][0] == "response"  # 用完即摘
    assert title_map["7687074567461915914"] == "标题A"
    assert publish_map["7687074567461915914"]  # create_time 转成时间串
    assert error == ""


# ---- bsk 形态 page 上纯 DOM 抓取 ----

def test_fetch_douyin_on_bsk_like_page_uses_dom_cards():
    page = FakeBskPage(body_text="破壁狗的主页 作品 喜欢")

    result = fetch_douyin_subscription_with_page(page, SOURCE, timeout_ms=60000, limit=12)

    assert result.ok and len(result.entries) == 2
    assert result.entries[0].link == "https://www.douyin.com/video/7687074567461915914"
    assert result.entries[0].title == "破壁狗：AI叛乱下：AI送给人类的文明寓言"
    assert page.goto_url == SOURCE["site_url"]


def test_fetch_douyin_dom_time_falls_back_to_now_when_card_has_no_time():
    """列表页卡片无时间元素：published 由 now_text() 兜底（非空、当天日期）。"""
    page = FakeBskPage()

    result = fetch_douyin_subscription_with_page(page, SOURCE, timeout_ms=60000, limit=12)

    assert result.ok
    for entry in result.entries:
        assert re.match(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}$", entry.published)


def test_fetch_douyin_bsk_page_never_touches_response_api():
    """假 bsk page 没有 on/remove_listener：若代码仍调用会 AttributeError。"""
    page = FakeBskPage()
    assert not hasattr(page, "on")

    result = fetch_douyin_subscription_with_page(page, SOURCE)

    assert result.ok


def test_dom_card_time_is_used_when_present():
    """卡片真带时间（如 time[datetime]）时仍走既有 DOM 解析路径。"""
    cards = [dict(DOM_CARDS[0], published="2026-09-19 19:30")]
    page = FakeBskPage(cards=cards)

    result = fetch_douyin_subscription_with_page(page, SOURCE, limit=12)

    assert result.ok and result.entries[0].published == "2026/09/19 19:30"


def test_normalize_douyin_published_accepts_detail_page_format():
    """详情页正文时间格式 YYYY-MM-DD HH:MM 与既有解析一致。"""
    assert _normalize_douyin_published("2026-09-19 19:30") == "2026/09/19 19:30"


# ---- 分派 ----

@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "rss_settings.json"

    def write(section: dict | None) -> None:
        path.write_text(json.dumps({"browser_backend": section} if section is not None else {}), encoding="utf-8")

    monkeypatch.setattr(rss_config, "SETTINGS_PATH", path)
    return write


class FakeBskSession:
    def __init__(self) -> None:
        self.started = 0
        self.shutdown_calls = 0
        self.page = object()

    def start(self) -> None:
        self.started += 1

    def shutdown(self, **kwargs) -> None:
        self.shutdown_calls += 1

    def acquire_page(self):
        return self.page


def _ok_result(source: dict) -> FeedFetchResult:
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=source["feed_url"],
        ok=True,
        status=200,
        entries=[FeedEntry(source["id"], source["name"], "标题", "https://www.douyin.com/video/1", "2026/09/19 19:30", "")],
    )


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("connectors._shared.bsk_session.BSK_RETRY_DELAY_SECONDS", 0.0)


def test_douyin_source_routes_to_bsk_session(settings_file):
    session = web_fetch._make_session_for(SOURCE)
    assert isinstance(session, BskRunnerSession)
    assert session.browser.session_id is None  # 懒启动，构造期不起子进程


def test_douyin_batch_uses_bsk(settings_file, monkeypatch):
    seen_sessions: list = []

    def fake_run_once(session, source, **kwargs):
        seen_sessions.append(session)
        return _ok_result(source)

    monkeypatch.setattr(web_fetch, "_make_session_for", lambda source: FakeBskSession())
    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_run_once)

    results = web_fetch.fetch_web_many([SOURCE])

    assert results[0].ok
    assert len(seen_sessions) == 1 and isinstance(seen_sessions[0], FakeBskSession)


def test_both_douyin_sources_are_bsk_only():
    settings = json.loads((REPO_ROOT / "config" / "rss_settings.json").read_text(encoding="utf-8"))
    sources = json.loads((REPO_ROOT / "config" / "subscription_sources.json").read_text(encoding="utf-8"))["sources"]
    douyin_ids = [s["id"] for s in sources if s.get("auth_key") == "douyin_shared"]

    assert set(douyin_ids) == {"破壁狗", "马儿聊世界"}
    # copy 后端已删除：配置里不再有可切换后端的字段
    assert "sources" not in settings["browser_backend"]
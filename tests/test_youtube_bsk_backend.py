"""YouTube 源迁到 bsk 后端：cookie 缺口的降级处理与后端分派。

假 page 打桩，不连真实 daemon / Chrome。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apps.subscriptions import rss_config
from connectors._shared import web_fetch
from connectors._shared.bsk_session import BskRunnerSession
from connectors.youtube import feed as yt_feed
from connectors.youtube.feed import _apply_youtube_language_pref, fetch_youtube_with_page

SOURCE = {
    "id": "laolihai_youtube",
    "name": "观点 外资观点 老厉害",
    "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC8gZZWIWmBuCb_gzC8DUrvw",
    "site_url": "https://www.youtube.com/channel/UC8gZZWIWmBuCb_gzC8DUrvw",
    "provider": "web",
    "fetch_via": "web",
}


class FakeContext:
    def __init__(self) -> None:
        self.cookies: list[dict] = []

    def add_cookies(self, cookies: list[dict]) -> None:
        self.cookies.extend(cookies)


class FakeCopyPage:
    """copy 后端形态：有 context.add_cookies。"""

    def __init__(self) -> None:
        self.context = FakeContext()
        self.goto_url = ""
        self.waited = []

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        self.waited.append(ms)

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, timeout=None, state="visible") -> None:
        return None

    def content(self) -> str:
        return _initial_data_html()


class FakeBskPage:
    """bsk 形态：没有 page.context（cookie 写入面缺失），其余调用面齐全。"""

    def __init__(self) -> None:
        self.goto_url = ""
        self.evaluate_calls = []

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        return None

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, timeout=None, state="visible") -> None:
        return None

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluate_calls.append(js)
        return []

    def evaluate_all(self, js, arg=None, **kwargs):
        return []

    def content(self) -> str:
        return _initial_data_html()


def _initial_data_html() -> str:
    payload = {
        "contents": {
            "gridVideoRenderer": {
                "videoId": "B8pVlybJ86g",
                "title": {"runs": [{"text": "标题A"}]},
                "publishedTimeText": {"simpleText": "8 hours ago"},
                "viewCountText": {"simpleText": "6K views"},
            }
        }
    }
    return "<html><script>var ytInitialData = " + json.dumps(payload, ensure_ascii=False) + ";</script></html>"


# ---- cookie 缺口 ----


def test_language_pref_sets_cookie_when_context_available():
    page = FakeCopyPage()

    _apply_youtube_language_pref(page)

    assert page.context.cookies == [
        {"name": "PREF", "value": "hl=en&gl=US", "domain": ".youtube.com", "path": "/"}
    ]


def test_language_pref_skips_silently_without_context():
    """bsk page 没有 context：不得抛错，靠 URL 查询参数达成同等语言/地区。"""
    page = FakeBskPage()

    _apply_youtube_language_pref(page)  # 不抛即通过


def test_fetch_youtube_on_bsk_like_page_succeeds_without_cookie_api():
    page = FakeBskPage()

    result = fetch_youtube_with_page(page, SOURCE)

    assert result.ok and len(result.entries) == 1
    assert result.entries[0].link == "https://www.youtube.com/watch?v=B8pVlybJ86g"
    assert "hl=en" in page.goto_url and "gl=US" in page.goto_url


def test_fetch_youtube_on_copy_page_still_sets_cookie():
    page = FakeCopyPage()

    result = fetch_youtube_with_page(page, SOURCE)

    assert result.ok and page.context.cookies


# ---- 分派 ----


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "rss_settings.json"

    def write(section: dict | None) -> None:
        path.write_text(json.dumps({"browser_backend": section} if section is not None else {}), encoding="utf-8")

    monkeypatch.setattr(rss_config, "SETTINGS_PATH", path)
    return write


def test_youtube_source_routes_to_bsk_session(settings_file, monkeypatch):
    settings_file({"default": "copy", "sources": {"laolihai_youtube": "bsk"}})
    monkeypatch.setattr(web_fetch, "resolve_web_target", lambda source: SimpleNamespace(site="youtube", page_url="https://x"))

    assert web_fetch._uses_bsk(SOURCE) is True
    session = web_fetch._make_session_for(SOURCE)
    assert isinstance(session, BskRunnerSession)
    # 构造期不得起 bsk 子进程（懒启动）
    assert session.browser.session_id is None


class FakeBskSession:
    """记录式假 bsk 会话，避免真的拉起 daemon。"""

    def __init__(self) -> None:
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def shutdown(self, **kwargs) -> None:
        return None


def test_youtube_batch_uses_bsk_and_never_starts_copy_session(settings_file, monkeypatch):
    settings_file({"default": "copy", "sources": {"laolihai_youtube": "bsk"}})
    started_copy: list = []
    seen_sessions: list = []

    def boom_copy():
        started_copy.append(True)
        raise AssertionError("youtube 配了 bsk 不得构造 copy 会话")

    def fake_run_once(session, source, **kwargs):
        seen_sessions.append(session)
        return yt_feed.FeedFetchResult(
            source["id"], source["name"], source["feed_url"], True, 200,
            [yt_feed.FeedEntry(source["id"], source["name"], "标题A", "https://www.youtube.com/watch?v=B8pVlybJ86g", "2026/09/22 00:11", "")],
        )

    monkeypatch.setattr(web_fetch, "_make_copy_session_for", boom_copy)
    monkeypatch.setattr(web_fetch, "_make_session_for", lambda source: FakeBskSession())
    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_run_once)

    results = web_fetch.fetch_web_many([SOURCE])

    assert results[0].ok
    assert len(seen_sessions) == 1 and isinstance(seen_sessions[0], FakeBskSession)
    assert started_copy == []
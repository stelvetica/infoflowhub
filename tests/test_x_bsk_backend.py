"""X（MacroMargin）源迁到 bsk 后端：反检测脚本探测式降级、调试产物降级与后端分派。

假 page 打桩，不连真实 daemon / Chrome。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apps.subscriptions import rss_config
from apps.subscriptions.models import FeedEntry
from connectors._shared import web_fetch
from connectors._shared.bsk_session import BskRunnerSession
from connectors.x.feed import (
    X_ANTI_DETECT_SCRIPT,
    _apply_x_anti_detect,
    _save_debug_assets,
    fetch_x_with_page,
)

SOURCE = {
    "id": "MacroMargin",
    "name": "观点 宏观 MacroMargin",
    "feed_url": "https://rsshub.app/twitter/user/MacroMargin",
    "site_url": "https://x.com/MacroMargin",
    "auth_key": "x_profile2",
    "provider": "web",
    "fetch_via": "web",
}


class FakeCopyPage:
    """copy 后端形态：有 add_init_script / screenshot。"""

    def __init__(self) -> None:
        self.init_scripts: list[str] = []
        self.screenshots: list[dict] = []

    def add_init_script(self, script) -> None:
        self.init_scripts.append(script)

    def screenshot(self, path=None, full_page=False) -> None:
        self.screenshots.append({"path": path, "full_page": full_page})


class FakeBskPage:
    """bsk 形态：无 add_init_script / screenshot，其余调用面齐全。"""

    def __init__(self) -> None:
        self.goto_url = ""
        self.wheeled: list[tuple] = []
        self.mouse = SimpleNamespace(wheel=lambda dx, dy: self.wheeled.append((dx, dy)))

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        return None

    def wait_for_selector(self, selector, timeout=None, state="visible") -> None:
        return None

    def locator(self, selector):
        return _FakeLocator(selector)

    def content(self) -> str:
        return "<html>bsk</html>"


class _FakeLocator:
    def __init__(self, selector: str) -> None:
        self.selector = selector

    def evaluate_all(self, js, arg=None, **kwargs):
        if self.selector == "article":
            return [
                {
                    "status_id": "2093621016576802932",
                    "href": "https://x.com/MacroMargin/status/2093621016576802932",
                    "published": "2026-09-23T06:44:06.000Z",
                    "text": "标题A\n摘要A",
                    "article_text": "标题A 摘要A",
                }
            ]
        return []

    def inner_text(self, timeout=None) -> str:
        return ""


# ---- add_init_script 探测式改写 ----


def test_anti_detect_injects_on_copy_page():
    page = FakeCopyPage()

    _apply_x_anti_detect(page)

    assert page.init_scripts == [X_ANTI_DETECT_SCRIPT]


def test_anti_detect_skips_silently_on_bsk_page():
    """bsk page 没有 add_init_script：不得抛 AttributeError。"""
    _apply_x_anti_detect(FakeBskPage())  # 不抛即通过


# ---- 调试产物降级 ----


def test_save_debug_assets_skips_screenshot_on_bsk_page(tmp_path, monkeypatch):
    monkeypatch.setattr("connectors.x.feed.DEBUG_DIR", tmp_path)
    page = FakeBskPage()

    _save_debug_assets(page, SOURCE, "empty")  # 不抛即通过

    written = sorted(p.suffix for p in tmp_path.iterdir())
    assert ".png" not in written  # 无 screenshot 写入面则跳过
    assert ".txt" in written and ".html" in written


def test_save_debug_assets_writes_screenshot_on_copy_page(tmp_path, monkeypatch):
    monkeypatch.setattr("connectors.x.feed.DEBUG_DIR", tmp_path)
    page = FakeCopyPage()

    _save_debug_assets(page, SOURCE)

    assert len(page.screenshots) == 1 and page.screenshots[0]["full_page"] is True


# ---- bsk 形态 page 上抓取不炸 ----


def test_fetch_x_on_bsk_like_page_parses_articles():
    page = FakeBskPage()

    result = fetch_x_with_page(page, SOURCE)

    assert result.ok and len(result.entries) == 1
    assert result.entries[0].link == "https://x.com/MacroMargin/status/2093621016576802932"
    assert result.entries[0].title == "标题A"


# ---- 分派 ----


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "rss_settings.json"

    def write(section: dict | None) -> None:
        path.write_text(json.dumps({"browser_backend": section} if section is not None else {}), encoding="utf-8")

    monkeypatch.setattr(rss_config, "SETTINGS_PATH", path)
    return write


def test_macromargin_source_routes_to_bsk_session(settings_file, monkeypatch):
    settings_file({"default": "copy", "sources": {"MacroMargin": "bsk"}})

    assert web_fetch._uses_bsk(SOURCE) is True
    session = web_fetch._make_session_for(SOURCE)
    assert isinstance(session, BskRunnerSession)
    # 构造期不得起 bsk 子进程（懒启动）
    assert session.browser.session_id is None


class FakeBskSession:
    def __init__(self) -> None:
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def shutdown(self, **kwargs) -> None:
        return None


def test_macromargin_batch_uses_bsk_and_never_starts_copy(settings_file, monkeypatch):
    settings_file({"default": "copy", "sources": {"MacroMargin": "bsk"}})
    started_copy: list = []
    seen_sessions: list = []

    def boom_copy():
        started_copy.append(True)
        raise AssertionError("MacroMargin 配了 bsk 不得构造 copy 会话")

    def fake_run_once(session, source, **kwargs):
        seen_sessions.append(session)
        return web_fetch.FeedFetchResult(
            source["id"], source["name"], source["feed_url"], True, 200,
            [FeedEntry(source["id"], source["name"], "标题A", "https://x.com/MacroMargin", "2026/09/23 06:44", "")],
        )

    monkeypatch.setattr(web_fetch, "_make_copy_session_for", boom_copy)
    monkeypatch.setattr(web_fetch, "_make_session_for", lambda source: FakeBskSession())
    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_run_once)

    results = web_fetch.fetch_web_many([SOURCE])

    assert results[0].ok
    assert len(seen_sessions) == 1 and isinstance(seen_sessions[0], FakeBskSession)
    assert started_copy == []
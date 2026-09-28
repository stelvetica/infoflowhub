"""laterhub「B 站稍后看」抓取器迁到 bsk 页内 fetch：假 page / 假会话打桩，不连真实 daemon。

覆盖：正常归一化与导航、自建会话生命周期（含异常路径）、失败必须显式抛 RuntimeError、
以及静态守卫（函数体不得再读 .env cookie / requests）。
"""

from __future__ import annotations

import inspect
import json

import pytest

from connectors.bilibili import api as bili_api

PAGE_URL = "https://www.bilibili.com/watchlater/list"

ITEM = {
    "bvid": "BV1xx411c7mD",
    "aid": 12345,
    "title": "示例稍后看视频",
    "owner": {"name": "某UP主"},
    "short_link_v2": "https://b23.tv/noise",
}
PAYLOAD = {"code": 0, "message": "0", "data": {"list": [dict(ITEM)]}}


class FakePage:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.goto_url = ""
        self.evaluated: list = []

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluated.append((js, arg, kwargs))
        return self.payload


class FakeSession:
    """外部会话：记录 start/shutdown，绝不期望被调用。"""

    def __init__(self, page) -> None:
        self.page = page
        self.start_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def shutdown(self, **kwargs) -> None:
        self.shutdown_calls += 1

    def acquire_page(self):
        return self.page


def _install_fake_bsk(monkeypatch, page):
    """把 api 模块里的 BskRunnerSession 换成假类，返回可读 created 字典。"""
    created: dict = {}

    class FakeBsk:
        def __init__(self, **kwargs) -> None:
            created["kwargs"] = kwargs
            created["instance"] = self
            self.page = page
            self.start_calls = 0
            self.shutdown_calls = 0

        def start(self) -> None:
            self.start_calls += 1

        def shutdown(self, **kwargs) -> None:
            self.shutdown_calls += 1

        def acquire_page(self):
            return self.page

    monkeypatch.setattr(bili_api, "BskRunnerSession", FakeBsk)
    return created


# ---- 正常路径 ----


def test_normal_path_normalizes_navigates_and_leaves_external_session_alone():
    page = FakePage(PAYLOAD)
    session = FakeSession(page)

    items = bili_api.fetch_bilibili_watchlater(session=session)

    assert len(items) == 1
    entry = items[0]
    assert entry["url"] == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert entry["title"] == "示例稍后看视频"
    assert entry["source"] == "bilibili_watchlater"
    assert entry["owner"] == "某UP主"
    assert entry["tags"] is None
    # 先导航到同域稍后看页（建立 Referer 上下文）
    assert page.goto_url == PAGE_URL
    # 外部传入的会话只使用、绝不 start/shutdown
    assert session.start_calls == 0
    assert session.shutdown_calls == 0
    # ps=20 保持既有行为，且走页内 fetch（evaluate）
    js, arg, kwargs = page.evaluated[0]
    assert json.loads(arg)["ps"] == 20
    assert "toview/web" in js


def test_normal_path_env_path_is_ignored(monkeypatch):
    """env_path 仅为签名兼容：传入也不得触碰 .env。"""
    page = FakePage(PAYLOAD)
    session = FakeSession(page)
    monkeypatch.setattr(
        bili_api, "load_project_env", lambda *a, **k: pytest.fail("不得读取 .env")
    )

    items = bili_api.fetch_bilibili_watchlater("C:/does/not/matter.env", session=session)

    assert len(items) == 1


# ---- 自建会话生命周期 ----


def test_self_builds_session_and_shuts_down(monkeypatch):
    page = FakePage(PAYLOAD)
    created = _install_fake_bsk(monkeypatch, page)

    items = bili_api.fetch_bilibili_watchlater()

    assert len(items) == 1
    assert created["kwargs"]["start_url"] == "about:blank"
    inst = created["instance"]
    assert inst.start_calls == 1
    assert inst.shutdown_calls == 1


def test_self_builds_session_shuts_down_on_exception(monkeypatch):
    class BoomPage(FakePage):
        def goto(self, url, wait_until=None, timeout=None):
            raise RuntimeError("navigate 失败")

    created = _install_fake_bsk(monkeypatch, BoomPage({}))

    with pytest.raises(RuntimeError) as ei:
        bili_api.fetch_bilibili_watchlater()

    assert "页内取数失败" in str(ei.value) and "navigate 失败" in str(ei.value)
    inst = created["instance"]
    assert inst.start_calls == 1
    assert inst.shutdown_calls == 1


def test_self_builds_session_shuts_down_on_evaluate_exception(monkeypatch):
    class BoomEvalPage(FakePage):
        def evaluate(self, js, arg=None, **kwargs):
            raise RuntimeError("evaluate 超时")

    created = _install_fake_bsk(monkeypatch, BoomEvalPage({}))

    with pytest.raises(RuntimeError, match="页内取数失败"):
        bili_api.fetch_bilibili_watchlater()

    inst = created["instance"]
    assert inst.start_calls == 1
    assert inst.shutdown_calls == 1


# ---- 明确失败 ----


def test_code_nonzero_raises_readable_error():
    page = FakePage({"code": -101, "message": "账号未登录", "data": None})
    session = FakeSession(page)

    with pytest.raises(RuntimeError) as ei:
        bili_api.fetch_bilibili_watchlater(session=session)

    text = str(ei.value)
    assert "code=-101" in text and "账号未登录" in text
    # 失败也不得动外部会话
    assert session.start_calls == 0 and session.shutdown_calls == 0


def test_non_json_raises_readable_error():
    page = FakePage({"error": "接口返回非 JSON（可能登录态失效）"})
    session = FakeSession(page)

    with pytest.raises(RuntimeError) as ei:
        bili_api.fetch_bilibili_watchlater(session=session)

    assert "非 JSON" in str(ei.value)


def test_empty_list_returns_empty_list_not_error():
    """空稍后看是合法结果：返回 []，可见性由 pipeline 的 0 条 WARN 承担，不得抛错。"""
    page = FakePage({"code": 0, "message": "0", "data": {"list": []}})
    session = FakeSession(page)

    assert bili_api.fetch_bilibili_watchlater(session=session) == []
    # 空列表也不得动外部会话
    assert session.start_calls == 0 and session.shutdown_calls == 0


def test_unrecognized_structure_raises():
    page = FakePage({"code": 0, "message": "0", "data": {"unexpected": 1}})
    session = FakeSession(page)

    with pytest.raises(RuntimeError) as ei:
        bili_api.fetch_bilibili_watchlater(session=session)

    assert "无法识别" in str(ei.value)


# ---- 静态守卫 ----


def test_source_no_longer_reads_env_cookie_or_http():
    src = inspect.getsource(bili_api.fetch_bilibili_watchlater)
    assert "BILIBILI_COOKIE" not in src
    assert "BilibiliConfig.from_env" not in src
    assert "requests" not in src

"""laterhub「抖音收藏」抓取器 bsk 后端：会话所有权、登录墙可见失败与静态守卫。

假 session / 假 page 打桩，不连真实 daemon 与 Chrome。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from connectors.douyin import favorites

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPO_ROOT / "connectors" / "douyin" / "favorites.py"

ITEMS = [
    {
        "video_id": "7687074567461915914",
        "url": "https://www.douyin.com/video/7687074567461915914?x=1",
        "title": "破壁狗：AI叛乱下",
        "raw_text": "921 破壁狗：AI叛乱下",
    },
    {
        "video_id": "7687074567461915914",
        "url": "https://www.douyin.com/video/7687074567461915914",
        "title": "重复项应被去重",
        "raw_text": "重复",
    },
    {
        "video_id": "999",
        "url": "https://www.douyin.com/note/999",
        "title": "非视频链接应被过滤",
        "raw_text": "",
    },
]


@pytest.fixture(autouse=True)
def _isolate_debug(tmp_path, monkeypatch):
    """调试 JSON 落到 tmp_path，跑测试绝不写用户 runtime 目录。"""
    monkeypatch.setattr(favorites, "DEBUG_JSON_PATH", tmp_path / "dbg_douyin.json")


class FakeMouse:
    """记录滚轮事件：懒加载必须靠真实 wheel（window.scrollTo 对内层滚动容器无效）。"""

    def __init__(self, ops: list) -> None:
        self._ops = ops
        self.wheel_calls: list = []

    def wheel(self, delta_x, delta_y) -> None:
        self.wheel_calls.append((delta_x, delta_y))
        self._ops.append(("wheel", delta_y))


class FakePage:
    """记录 goto / wait_for_timeout / evaluate / wheel 调用并返回构造好的 items。"""

    def __init__(self, items=None, body_text="", raise_on_goto=False) -> None:
        self.items = ITEMS if items is None else items
        self.body_text = body_text
        self.raise_on_goto = raise_on_goto
        self.goto_calls: list = []
        self.wait_calls: list = []
        self.evaluate_calls: list = []
        self.ops: list = []
        self.mouse = FakeMouse(self.ops)
        self.closed = 0

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        self.ops.append(("goto", url))
        if self.raise_on_goto:
            raise RuntimeError("navigate boom")

    def wait_for_timeout(self, ms) -> None:
        self.wait_calls.append(ms)
        self.ops.append(("wait", ms))

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluate_calls.append((js, arg, kwargs))
        self.ops.append(("evaluate", js))
        if "innerText" in js:
            return self.body_text
        return self.items

    def close(self) -> None:
        self.closed += 1


class FakeSession:
    def __init__(self, page) -> None:
        self._page = page
        self.start_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def shutdown(self, **kwargs) -> None:
        self.shutdown_calls += 1

    def acquire_page(self):
        return self._page


def test_external_session_is_used_but_never_started_or_shutdown():
    page = FakePage()
    session = FakeSession(page)

    result = favorites.fetch_douyin_favorites(session=session)

    # 归一化 + 去重：同视频 id 的重复项与 /note/ 链接被剔除
    assert len(result) == 1
    assert result[0]["url"] == "https://www.douyin.com/video/7687074567461915914"
    assert result[0]["title"] == "破壁狗：AI叛乱下"
    assert result[0]["source"] == "douyin_favorite"
    # 页面操作确实发生
    assert page.goto_calls and page.goto_calls[0][0] == favorites.DOUYIN_FAVORITE_URL
    # 首屏等待 → 提取 → 滚一轮（假 page 每轮条数不增长，故滚一轮即停）→ 取 body 文本
    assert page.wait_calls == [5000, favorites.DOUYIN_SCROLL_WAIT_MS]
    assert [op[0] for op in page.ops[:6]] == ["goto", "wait", "evaluate", "wheel", "wait", "evaluate"]
    assert page.ops[0][1] == favorites.DOUYIN_FAVORITE_URL
    assert page.ops[1][1] == 5000
    # 第 4 个操作必须是真实滚轮事件（提取→滚动→等待→再提取，顺序不能颠倒）
    assert page.ops[3] == ("wheel", favorites.DOUYIN_SCROLL_STEP_PX)
    # 滚动前后的两次 evaluate 都是抽取 JS，不是滚动表达式
    assert "querySelectorAll" in page.ops[2][1]
    assert "querySelectorAll" in page.ops[5][1]
    assert page.evaluate_calls
    # 懒加载必须用 wheel：window.scrollTo 对抖音内层滚动容器几乎无效（实测 36 vs 134 锚点）
    assert page.mouse.wheel_calls == [(0, favorites.DOUYIN_SCROLL_STEP_PX)]
    assert all("scrollTo" not in call[0] for call in page.evaluate_calls)
    # 外部会话所有权：只使用，绝不 start/shutdown
    assert session.start_calls == 0
    assert session.shutdown_calls == 0


def test_scroll_loop_stops_early_and_keeps_growing_until_rounds_exhausted():
    """每轮条数增长就继续滚，直到上限；条数不再增长则提前停。"""

    class GrowingPage(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.extract_rounds = 0

        def evaluate(self, js, arg=None, **kwargs):
            if "innerText" in js:
                return super().evaluate(js, arg, **kwargs)
            self.evaluate_calls.append((js, arg, kwargs))
            self.ops.append(("evaluate", js))
            self.extract_rounds += 1
            # 每轮多一条，永远增长 → 必须恰好滚满上限轮数
            return self.items + [
                {"url": f"https://www.douyin.com/video/{i}", "title": f"t{i}"}
                for i in range(self.extract_rounds)
            ]

    page = GrowingPage()
    favorites.fetch_douyin_favorites(session=FakeSession(page))

    # 首屏 1 次 + 每轮 1 次 = 1 + DOUYIN_SCROLL_ROUNDS
    assert page.extract_rounds == 1 + favorites.DOUYIN_SCROLL_ROUNDS
    wheel_ops = [op for op in page.ops if op[0] == "wheel"]
    assert len(wheel_ops) == favorites.DOUYIN_SCROLL_ROUNDS


def test_own_session_started_and_shutdown_once(monkeypatch):
    page = FakePage()
    session = FakeSession(page)

    def fake_ctor(**kwargs):
        assert kwargs.get("start_url") == "about:blank"
        return session

    monkeypatch.setattr(favorites, "BskRunnerSession", fake_ctor)

    result = favorites.fetch_douyin_favorites()

    assert len(result) == 1
    assert session.start_calls == 1
    assert session.shutdown_calls == 1


def test_own_session_shutdown_on_exception(monkeypatch):
    page = FakePage(raise_on_goto=True)
    session = FakeSession(page)
    monkeypatch.setattr(favorites, "BskRunnerSession", lambda **kwargs: session)

    with pytest.raises(RuntimeError, match="navigate boom"):
        favorites.fetch_douyin_favorites()

    assert session.start_calls == 1
    assert session.shutdown_calls == 1


def test_login_wall_raises_visible_failure():
    page = FakePage(items=[], body_text="请扫码登录后查看")
    session = FakeSession(page)

    with pytest.raises(RuntimeError) as excinfo:
        favorites.fetch_douyin_favorites(session=session)

    assert "登录" in str(excinfo.value)


def test_empty_without_login_wall_returns_empty_list():
    page = FakePage(items=[], body_text="我的收藏")
    session = FakeSession(page)

    assert favorites.fetch_douyin_favorites(session=session) == []


def test_non_list_extract_result_fails_loudly():
    """复刻真机症状：页内函数没被执行时 bsk 返回 {}（函数按值序列化），不得静默当 0 条。"""
    page = FakePage(items={})
    session = FakeSession(page)

    with pytest.raises(RuntimeError, match="异常类型: dict"):
        favorites.fetch_douyin_favorites(session=session)


def test_extract_js_is_a_function_expression_that_the_facade_must_invoke():
    """提取 JS 是裸函数表达式字符串：bsk 不会自动调用，必须靠 BskPage.evaluate 包装。"""
    from connectors._shared.bsk_browser import BskPage

    class RecordingBrowser:
        def __init__(self) -> None:
            self.expressions: list[str] = []

        def evaluate(self, expression, **kwargs):
            self.expressions.append(expression)
            return [{"video_id": "1", "url": "https://www.douyin.com/video/1", "title": "t", "raw_text": ""}]

    browser = RecordingBrowser()
    items = favorites._extract_visible_items(BskPage(browser))

    assert [item["video_id"] for item in items] == ["1"]
    expr = browser.expressions[0]
    assert "() => {" in expr  # 原函数体原样嵌入
    assert "typeof f==='function'?f():f" in expr  # 且外面套了「是函数就调用」的包装
    assert expr.rstrip().endswith("))")


def test_source_no_longer_references_copy_runner():
    source = SOURCE_PATH.read_text(encoding="utf-8")
    assert "CopyRunnerSession" not in source
    assert "LATERHUB_RUNNER_DIR" not in source
    assert "9224" not in source

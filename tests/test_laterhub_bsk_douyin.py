"""laterhub「抖音收藏」抓取器 bsk 后端：会话所有权、登录墙可见失败、增量/深度滚动契约。

假 session / 假 page 打桩，不连真实 daemon 与 Chrome。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from connectors._shared.common import FetchBatch
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


def _video(n: int) -> dict:
    return {
        "video_id": str(n),
        "url": f"https://www.douyin.com/video/{n}",
        "title": f"作品{n}",
        "raw_text": "",
    }


@pytest.fixture(autouse=True)
def _isolate_debug(tmp_path, monkeypatch):
    """调试 JSON 落到 tmp_path，跑测试绝不写用户 runtime 目录。"""
    monkeypatch.setattr(favorites, "DEBUG_JSON_PATH", tmp_path / "dbg_douyin.json")


def _debug() -> dict:
    return json.loads(favorites.DEBUG_JSON_PATH.read_text(encoding="utf-8"))


class FakeMouse:
    """记录滚轮事件：懒加载必须靠真实 wheel（window.scrollTo 对内层滚动容器无效）。"""

    def __init__(self, page: "FakePage") -> None:
        self._page = page
        self.wheel_calls: list = []

    def wheel(self, delta_x, delta_y) -> None:
        self.wheel_calls.append((delta_x, delta_y))
        self._page.ops.append(("wheel", delta_y))
        self._page.scroll_top = min(
            self._page.scroll_top + delta_y,
            self._page.scroll_height - self._page.scroll_client,
        )


class FakePage:
    """记录 goto / wait_for_timeout / evaluate / wheel 调用并返回构造好的 items。

    round_items 可按提取轮次返回不同条目：列表（越界取最后一项）或 `idx -> list` 可调用对象。
    提取轮次 0 是首屏，之后每滚一轮 +1。
    """

    def __init__(self, items=None, body_text="", raise_on_goto=False, round_items=None) -> None:
        self.items = ITEMS if items is None else items
        self.body_text = body_text
        self.raise_on_goto = raise_on_goto
        self.round_items = round_items
        self.goto_calls: list = []
        self.wait_calls: list = []
        self.evaluate_calls: list = []
        self.extract_rounds = 0
        self.ops: list = []
        self.mouse = FakeMouse(self)
        self.closed = 0
        # 滚动几何：深度模式判到底的依据（视口 500，内容 4000 → 可滚 3500）
        self.scroll_top = 0
        self.scroll_height = 4000
        self.scroll_client = 500

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        self.ops.append(("goto", url))
        if self.raise_on_goto:
            raise RuntimeError("navigate boom")

    def wait_for_timeout(self, ms) -> None:
        self.wait_calls.append(ms)
        self.ops.append(("wait", ms))

    def _round_items(self) -> list:
        idx = self.extract_rounds
        self.extract_rounds += 1
        if self.round_items is None:
            return self.items
        if callable(self.round_items):
            return self.round_items(idx)
        return self.round_items[min(idx, len(self.round_items) - 1)]

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluate_calls.append((js, arg, kwargs))
        self.ops.append(("evaluate", js))
        if "innerText" in js:
            return self.body_text
        if "HTMLMediaElement" in js:
            return "installed"
        if "scrollHeight" in js:
            return {"top": self.scroll_top, "height": self.scroll_height, "client": self.scroll_client}
        return self._round_items()

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

    batch = favorites.fetch_douyin_favorites(session=session)

    assert isinstance(batch, FetchBatch)
    # 归一化 + 去重：同视频 id 的重复项与 /note/ 链接被剔除
    assert len(batch.items) == 1
    assert batch.items[0]["url"] == "https://www.douyin.com/video/7687074567461915914"
    assert batch.items[0]["title"] == "破壁狗：AI叛乱下"
    assert batch.items[0]["source"] == "douyin_favorite"
    # 增量模式即使已滚到底也不参与移除判定
    assert batch.complete is False
    # 页面操作确实发生
    assert page.goto_calls and page.goto_calls[0][0] == favorites.DOUYIN_FAVORITE_URL
    # 首屏等待 → 停播器 → 提取 → 连续两轮零新增才停 → 取 body 文本
    assert page.wait_calls[0] == 5000
    assert len(page.wait_calls) == 1 + favorites.DOUYIN_SCROLL_EMPTY_ROUNDS
    assert [op[0] for op in page.ops[:6]] == ["goto", "wait", "evaluate", "evaluate", "wheel", "wait"]
    assert page.ops[0][1] == favorites.DOUYIN_FAVORITE_URL
    assert page.ops[1][1] == 5000
    # 停播器必须在第一次滚动之前装上：滚轮会触发卡片预览自动播放，媒体源一变后续动作全被拒
    assert "HTMLMediaElement" in page.ops[2][1]
    # 第 5 个操作必须是真实滚轮事件（提取→滚动→等待→再提取，顺序不能颠倒）
    assert page.ops[4] == ("wheel", favorites.DOUYIN_SCROLL_STEP_PX)
    # 停播器之后、滚动前后的 evaluate 都是抽取 JS，不是滚动表达式
    assert "querySelectorAll" in page.ops[3][1]
    assert "querySelectorAll" in page.ops[6][1]
    # 懒加载必须用 wheel：window.scrollTo 对抖音内层滚动容器几乎无效（实测 36 vs 134 锚点）
    assert page.mouse.wheel_calls == [(0, favorites.DOUYIN_SCROLL_STEP_PX)] * 2
    assert all("scrollTo" not in call[0] for call in page.evaluate_calls)
    # 调试产物如实记录增量语义
    debug = _debug()
    assert debug["mode"] == "incremental"
    assert debug["rounds"] == 2
    assert debug["reached_bottom"] is True
    assert debug["complete"] is False
    # 外部会话所有权：只使用，绝不 start/shutdown
    assert session.start_calls == 0
    assert session.shutdown_calls == 0


def test_incremental_stops_only_after_two_consecutive_empty_rounds():
    """追新增长一轮后连续两轮零新增才停：单轮零新增可能是虚拟列表抖动，不能据此收工。"""
    # idx0 首屏；idx1 多一条；idx2、idx3 相同（连续两轮零新增）
    rounds = [ITEMS, ITEMS + [_video(1)], ITEMS + [_video(1)], ITEMS + [_video(1)]]
    page = FakePage(round_items=rounds)

    batch = favorites.fetch_douyin_favorites(session=FakeSession(page))

    assert len(page.mouse.wheel_calls) == 3  # 增长轮 + 两轮零新增
    assert page.extract_rounds == 4  # 首屏 + 3 轮
    assert {item["url"] for item in batch.items} == {
        "https://www.douyin.com/video/7687074567461915914",
        "https://www.douyin.com/video/1",
    }
    debug = _debug()
    assert debug["rounds"] == 3
    assert debug["reached_bottom"] is True
    assert batch.complete is False


def test_incremental_growing_always_rolls_max_rounds():
    """每轮都有新增 → 滚满增量上限即停，不无限滚。"""
    page = FakePage(round_items=lambda idx: ITEMS + [_video(i) for i in range(idx)])

    batch = favorites.fetch_douyin_favorites(session=FakeSession(page))

    assert len(page.mouse.wheel_calls) == favorites.DOUYIN_SCROLL_ROUNDS
    assert page.extract_rounds == 1 + favorites.DOUYIN_SCROLL_ROUNDS
    assert batch.complete is False
    # 累积去重：首屏 1 条有效 + 每轮各 1 条新增
    assert len(batch.items) == 1 + favorites.DOUYIN_SCROLL_ROUNDS
    debug = _debug()
    assert debug["rounds"] == favorites.DOUYIN_SCROLL_ROUNDS
    assert debug["reached_bottom"] is False


def test_incremental_is_never_complete_even_when_reaching_bottom():
    page = FakePage(round_items=[ITEMS, ITEMS])

    batch = favorites.fetch_douyin_favorites(session=FakeSession(page))

    debug = _debug()
    assert debug["reached_bottom"] is True
    assert batch.complete is False


def test_deep_scan_with_zero_items_is_not_complete():
    """0 条更像页面没渲染/结构变了：绝不能当「收藏全清空」把所有库内条目判成已移除。"""
    page = FakePage(items=[], body_text="我的收藏")

    batch = favorites.fetch_douyin_favorites(session=FakeSession(page), deep=True)

    debug = _debug()
    assert debug["reached_bottom"] is True
    assert batch.items == []
    assert batch.complete is False


def test_deep_scan_collects_until_bottom_and_marks_complete():
    """深度：增长几轮后连续零新增 = 到底 → complete=True，且逐步长（900）避免跳过卡片。"""
    rounds = [
        ITEMS,
        ITEMS + [_video(1)],
        ITEMS + [_video(1), _video(2)],
        ITEMS + [_video(1), _video(2), _video(3)],
    ]
    page = FakePage(round_items=rounds)

    batch = favorites.fetch_douyin_favorites(session=FakeSession(page), deep=True)

    assert page.mouse.wheel_calls == [(0, favorites.DOUYIN_DEEP_SCROLL_STEP_PX)] * 5
    # 首屏 1 条 + 新增 3 条，且必须全部保留（跨轮累积，不能每轮覆盖）
    assert len(batch.items) == 4
    assert batch.complete is True
    debug = _debug()
    assert debug["mode"] == "deep"
    assert debug["rounds"] == 5
    assert debug["reached_bottom"] is True
    assert debug["complete"] is True


def test_deep_scan_hitting_round_cap_is_incomplete():
    """始终增长 → 撞 200 轮上限 = 没拿全 → complete=False（不参与移除判定）。"""
    page = FakePage(round_items=lambda idx: ITEMS + [_video(i) for i in range(idx)])

    batch = favorites.fetch_douyin_favorites(session=FakeSession(page), deep=True)

    assert len(page.mouse.wheel_calls) == favorites.DOUYIN_DEEP_SCROLL_MAX_ROUNDS
    assert batch.complete is False
    debug = _debug()
    assert debug["rounds"] == favorites.DOUYIN_DEEP_SCROLL_MAX_ROUNDS
    assert debug["reached_bottom"] is False


def test_scroll_wait_grows_with_round_and_is_capped(monkeypatch):
    monkeypatch.setattr(favorites.random, "randint", lambda low, high: 0)
    page = FakePage(round_items=lambda idx: ITEMS + [_video(i) for i in range(idx)])

    favorites.fetch_douyin_favorites(session=FakeSession(page))

    waits = page.wait_calls[1:]
    expected = [
        min(
            favorites.DOUYIN_SCROLL_WAIT_BASE_MS + round_no * favorites.DOUYIN_SCROLL_WAIT_STEP_MS,
            favorites.DOUYIN_SCROLL_WAIT_MAX_MS,
        )
        for round_no in range(favorites.DOUYIN_SCROLL_ROUNDS)
    ]
    assert waits == expected
    assert waits == sorted(waits)


def test_own_session_started_and_shutdown_once(monkeypatch):
    page = FakePage()
    session = FakeSession(page)

    def fake_ctor(**kwargs):
        assert kwargs.get("start_url") == "about:blank"
        return session

    monkeypatch.setattr(favorites, "BskRunnerSession", fake_ctor)

    batch = favorites.fetch_douyin_favorites()

    assert len(batch.items) == 1
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


def test_empty_without_login_wall_returns_empty_batch():
    page = FakePage(items=[], body_text="我的收藏")
    session = FakeSession(page)

    batch = favorites.fetch_douyin_favorites(session=session)

    assert batch.items == []
    assert batch.complete is False


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


def test_incremental_stops_when_known_streak_reached():
    """日常短抓取：首屏就连续撞到 3 条已知 → 已追平历史，不必再滚。"""
    known_urls = [f"https://www.douyin.com/video/{n}" for n in (1, 2, 3)]
    page = FakePage(round_items=[[_video(n) for n in (1, 2, 3)]])

    batch = favorites.fetch_douyin_favorites(
        session=FakeSession(page), known_urls=known_urls
    )

    assert page.mouse.wheel_calls == []
    assert page.extract_rounds == 1
    assert batch.complete is False

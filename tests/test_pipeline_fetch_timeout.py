"""laterhub 流水线 bsk 会话生命周期与超时兜底测试。

覆盖：正常/异常路径、超时停会话并隔离、外部会话不被 shutdown、超时后后续源仍调度、
0 条 WARN 可见性，以及旧 copy 符号的静态守卫。全部打桩，不连真实 daemon / Chrome。
"""

from __future__ import annotations

import argparse
import time
import types
from pathlib import Path

import pytest

from apps.laterhub import pipeline

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_PATH = REPO_ROOT / "apps" / "laterhub" / "pipeline.py"


@pytest.fixture(autouse=True)
def _isolate_laterhub_health(monkeypatch):
    """pipeline 抓取后会写健康文件：打桩模块级名字，避免污染真实 runtime。"""
    monkeypatch.setattr(pipeline, "record_result", lambda *args, **kwargs: None)


class FakeDB:
    def __init__(self) -> None:
        self.saved: list = []

    def upsert_link(self, record) -> None:
        self.saved.append(record)


class FakeSession:
    def __init__(self) -> None:
        self.start_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def shutdown(self, **kwargs) -> None:
        self.shutdown_calls += 1


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        retry_failed=False,
        fetch_bilibili=False,
        fetch_douyin=False,
        fetch_xiaoheihe=False,
        fetch_weread=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_fetcher_normal_returns_result():
    """正常 fetcher：看门狗放行结果。"""

    def ok_fetcher(env_path, *, session=None):
        return [{"url": "u1", "title": "t1", "source": "s1"}]

    got = pipeline._run_fetcher_with_timeout("测试源", ok_fetcher)
    assert got == [{"url": "u1", "title": "t1", "source": "s1"}]


def test_fetcher_timeout_stops_owned_session_and_raises(monkeypatch):
    """挂死 fetcher：超时后停自建会话并抛 RuntimeError，不被卡死。"""
    monkeypatch.setattr(pipeline, "SOURCE_FETCH_TIMEOUT_SECONDS", 1)
    session = FakeSession()

    def hang_fetcher(env_path, *, session=None):
        time.sleep(60)
        return []

    with pytest.raises(RuntimeError) as excinfo:
        pipeline._run_fetcher_with_timeout("小黑盒收藏", hang_fetcher, session, owns_session=True)

    message = str(excinfo.value)
    assert "小黑盒收藏" in message
    assert str(pipeline.SOURCE_FETCH_TIMEOUT_SECONDS) in message
    assert session.shutdown_calls == 1


def test_fetcher_timeout_leaves_external_session_alone(monkeypatch):
    """超时但会话归调用方所有：不得 shutdown。"""
    monkeypatch.setattr(pipeline, "SOURCE_FETCH_TIMEOUT_SECONDS", 1)
    session = FakeSession()

    def hang_fetcher(env_path, *, session=None):
        time.sleep(60)
        return []

    with pytest.raises(RuntimeError):
        pipeline._run_fetcher_with_timeout("抖音收藏", hang_fetcher, session, owns_session=False)

    assert session.shutdown_calls == 0


def test_fetcher_exception_propagates(monkeypatch):
    """fetcher 内部抛错：原样上传，不触发会话 shutdown。"""
    session = FakeSession()

    def boom_fetcher(env_path, *, session=None):
        raise ValueError("boom")

    with pytest.raises(ValueError) as excinfo:
        pipeline._run_fetcher_with_timeout("抖音收藏", boom_fetcher, session, owns_session=True)

    assert str(excinfo.value) == "boom"
    assert session.shutdown_calls == 0


def test_timeout_does_not_block_next_source(monkeypatch):
    """第一个源超时后：停掉该会话恰一次，第二个源拿到全新会话并正常返回。

    hermetic：patch 掉 pipeline.BskRunnerSession，绝不真构造/start bsk 会话。
    """
    monkeypatch.setattr(pipeline, "SOURCE_FETCH_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)

    created: list = []
    received: list = []

    class FakeBsk:
        def __init__(self, *, start_url="about:blank") -> None:
            self.browser = types.SimpleNamespace(session_id=f"s{len(created)}")
            self.start_calls = 0
            self.shutdown_calls = 0
            created.append(self)

        def start(self) -> None:
            self.start_calls += 1

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            self.browser.session_id = None

    def hang_fetcher(env_path, *, session=None):
        received.append(session)
        time.sleep(60)
        return []

    def ok_fetcher(env_path, *, session=None):
        received.append(session)
        if session is None:
            # 模拟真实抓取器：无共享会话时自建一个。
            own = pipeline.BskRunnerSession(start_url="about:blank")
            own.start()
            own.shutdown()
        return [{"url": "u2", "title": "t2", "source": "xiaoheihe_favor"}]

    monkeypatch.setattr(pipeline, "BskRunnerSession", FakeBsk)
    monkeypatch.setattr(pipeline, "fetch_douyin_favorites", hang_fetcher)
    monkeypatch.setattr(pipeline, "fetch_xiaoheihe_favorites", ok_fetcher)

    db = FakeDB()
    scheduled = pipeline._fetch_enabled_sources(
        args=_args(fetch_douyin=True, fetch_xiaoheihe=True), db=db, session=None
    )

    assert scheduled is True
    assert [record.url for record in db.saved] == ["u2"]
    # 第一源拿到 pipeline 自建会话，超时后该会话被 stop 恰一次。
    assert received[0] is created[0]
    assert created[0].shutdown_calls == 1
    # 第二源：pipeline 手里的会话已被置空，抓取器自建全新会话（不是被停掉的那个）。
    assert received[1] is None
    assert len(created) == 2
    assert created[1] is not created[0]
    assert created[1].start_calls == 1


def test_dead_session_from_internal_timeout_is_rebuilt_for_next_source(monkeypatch):
    """H1 回归：源 1 内部超时把共享会话 stop 掉但正常返回，源 2 必须拿到新会话而非死会话。"""
    messages: list[str] = []
    monkeypatch.setattr(pipeline, "log_line", messages.append)

    created: list = []
    received: list = []

    class FakeBsk:
        def __init__(self, *, start_url="about:blank") -> None:
            self.browser = types.SimpleNamespace(session_id=f"s{len(created)}")
            self.start_calls = 0
            self.shutdown_calls = 0
            created.append(self)

        def start(self) -> None:
            self.start_calls += 1

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            self.browser.session_id = None

    def dying_fetcher(env_path, *, session=None):
        received.append(session)
        # 模拟 BskBrowser 子进程超时：默认 stop_session_on_timeout 会清空 session_id，但抓取器正常返回。
        session.browser.session_id = None
        return []

    def ok_fetcher(env_path, *, session=None):
        received.append(session)
        return [{"url": "u2", "title": "t2", "source": "xiaoheihe_favor"}]

    monkeypatch.setattr(pipeline, "BskRunnerSession", FakeBsk)
    monkeypatch.setattr(pipeline, "fetch_douyin_favorites", dying_fetcher)
    monkeypatch.setattr(pipeline, "fetch_xiaoheihe_favorites", ok_fetcher)

    db = FakeDB()
    scheduled = pipeline._fetch_enabled_sources(
        args=_args(fetch_douyin=True, fetch_xiaoheihe=True), db=db, session=None
    )

    assert scheduled is True
    assert [record.url for record in db.saved] == ["u2"]
    # 源 1 会话失效 → 置空，源 2 不再复用死会话。
    assert received[0] is created[0]
    assert received[1] is None
    assert any("会话已失效" in message for message in messages)
    # finally 只对仍存活的会话 shutdown；源 1 会话已失效置空，故不再被 shutdown。
    assert created[0].shutdown_calls == 0


def test_self_built_session_started_and_shutdown_once(monkeypatch):
    """自建会话：三源共用同一个，start/shutdown 各一次。"""
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)
    created: list = []

    class FakeBsk:
        def __init__(self, *, start_url="about:blank") -> None:
            self.start_url = start_url
            self.browser = types.SimpleNamespace(session_id="shared")
            self.start_calls = 0
            self.shutdown_calls = 0
            created.append(self)

        def start(self) -> None:
            self.start_calls += 1

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            self.browser.session_id = None

    monkeypatch.setattr(pipeline, "BskRunnerSession", FakeBsk)
    for name, source in (
        ("fetch_bilibili_watchlater", "bilibili_watchlater"),
        ("fetch_douyin_favorites", "douyin_favorite"),
        ("fetch_xiaoheihe_favorites", "xiaoheihe_favor"),
    ):
        monkeypatch.setattr(
            pipeline,
            name,
            lambda env_path, *, session=None, source=source: [
                {"url": f"u-{source}", "title": "t", "source": source}
            ],
        )

    pipeline._fetch_enabled_sources(
        args=_args(fetch_bilibili=True, fetch_douyin=True, fetch_xiaoheihe=True),
        db=FakeDB(),
        session=None,
    )

    assert len(created) == 1
    assert created[0].start_url == "about:blank"
    assert created[0].start_calls == 1
    assert created[0].shutdown_calls == 1


def test_self_built_session_shuts_down_on_fetcher_exception(monkeypatch):
    """异常路径：fetcher 抛错也要 shutdown 自建会话。"""
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)
    created: list = []

    class FakeBsk:
        def __init__(self, *, start_url="about:blank") -> None:
            self.browser = types.SimpleNamespace(session_id="s1")
            self.start_calls = 0
            self.shutdown_calls = 0
            created.append(self)

        def start(self) -> None:
            self.start_calls += 1

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            self.browser.session_id = None

    def boom_fetcher(env_path, *, session=None):
        raise ValueError("boom")

    monkeypatch.setattr(pipeline, "BskRunnerSession", FakeBsk)
    monkeypatch.setattr(pipeline, "fetch_douyin_favorites", boom_fetcher)

    pipeline._fetch_enabled_sources(args=_args(fetch_douyin=True), db=FakeDB(), session=None)

    assert created[0].start_calls == 1
    assert created[0].shutdown_calls == 1


def test_self_built_session_start_failure_marks_sources_failed_without_crash(monkeypatch):
    """会话 start 失败：三源各记失败并继续走完流程，不得向上抛炸穿整批。"""
    messages: list[str] = []
    monkeypatch.setattr(pipeline, "log_line", messages.append)

    class BrokenBsk:
        def __init__(self, *, start_url="about:blank") -> None:
            self.shutdown_calls = 0

        def start(self) -> None:
            raise RuntimeError("daemon 未就绪")

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    monkeypatch.setattr(pipeline, "BskRunnerSession", BrokenBsk)

    scheduled = pipeline._fetch_enabled_sources(
        args=_args(fetch_douyin=True, fetch_xiaoheihe=True), db=FakeDB(), session=None
    )

    assert scheduled is True
    assert sum("抓取失败" in message for message in messages) == 2


def test_bsk_construction_failure_marks_sources_failed_without_crash(monkeypatch):
    """H2 回归：bsk CLI 缺失使构造即抛错时，异常不得穿透 pipeline，三源各记失败并继续走完流程。"""
    messages: list[str] = []
    monkeypatch.setattr(pipeline, "log_line", messages.append)

    class NoCliBsk:
        def __init__(self, *, start_url="about:blank") -> None:
            raise RuntimeError("未找到 bsk CLI")

    monkeypatch.setattr(pipeline, "BskRunnerSession", NoCliBsk)

    scheduled = pipeline._fetch_enabled_sources(
        args=_args(fetch_douyin=True, fetch_xiaoheihe=True), db=FakeDB(), session=None
    )

    assert scheduled is True
    assert sum("抓取失败" in message for message in messages) == 2


def test_external_session_not_started_or_shutdown(monkeypatch):
    """外部传入会话：只复用，绝不 start/shutdown。"""
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)
    session = FakeSession()
    monkeypatch.setattr(
        pipeline,
        "fetch_douyin_favorites",
        lambda env_path, *, session=None: [{"url": "u1", "title": "t1", "source": "douyin_favorite"}],
    )

    pipeline._fetch_enabled_sources(args=_args(fetch_douyin=True), db=FakeDB(), session=session)

    assert session.start_calls == 0
    assert session.shutdown_calls == 0


def test_zero_items_logs_explicit_warn(monkeypatch):
    """抓取完成但 0 条：必须打明确 WARN，不能只记新增/更新 0 条。"""
    from apps.laterhub.sources import SOURCE_BY_ID

    messages: list[str] = []
    monkeypatch.setattr(pipeline, "log_line", messages.append)

    pipeline._fetch_source(
        source=SOURCE_BY_ID["douyin_favorite"],
        fetcher=lambda env_path, *, session=None: [],
        db=FakeDB(),
        session=None,
    )

    warn_lines = [message for message in messages if "WARN" in message]
    assert warn_lines, messages
    assert "0 条" in warn_lines[0]


def test_copy_symbols_removed():
    """旧的 copy 专用兜底符号必须已删除。"""
    assert not hasattr(pipeline, "_terminate_source_session")
    assert not hasattr(pipeline, "_source_debug_port")


def test_source_has_no_copy_tokens():
    """静态守卫：pipeline 源码不得残留 copy 语义痕迹。"""
    source = PIPELINE_PATH.read_text(encoding="utf-8")
    for token in (
        "CopyRunnerSession",
        "LATERHUB_RUNNER_DIR",
        "close_debug_browser",
        "_reopen_user_chrome",
        "9224",
        "9225",
    ):
        assert token not in source, f"pipeline.py 仍残留旧 copy 痕迹: {token}"

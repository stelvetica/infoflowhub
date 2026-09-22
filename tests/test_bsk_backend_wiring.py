"""蓝宝书/网页源接 bsk 后端的接线测试：后端选择、回退、单次落库、配置缺省。

复用 tests/test_bsk_browser.py 的假 bsk CLI 模式，不依赖真实 daemon/Chrome。
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from apps.subscriptions import rss_config
from connectors._shared import web_fetch
from connectors._shared.bsk_browser import BskCommandError
from connectors._shared.bsk_session import BskRunnerSession
from connectors.alphapai import runner as alphapai_runner
from test_bsk_browser import FAKE_CLI

ALPHAPAI_URL = "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"
SOURCE = {
    "id": "alphapai",
    "name": "资讯 Alpha派蓝宝书",
    "feed_url": ALPHAPAI_URL,
    "site_url": ALPHAPAI_URL,
    "provider": "web",
    "fetch_via": "web",
}


class FakeCopySession:
    """占位复制副本会话：记录实例，绝不真起 Chrome。"""

    instances: list["FakeCopySession"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.started = False
        self.shutdown_calls = 0
        FakeCopySession.instances.append(self)

    def start(self) -> None:
        self.started = True

    def shutdown(self, **kwargs) -> None:
        self.started = False
        self.shutdown_calls += 1

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()
        return False


@pytest.fixture(autouse=True)
def _reset_fake_copy():
    FakeCopySession.instances = []
    yield
    FakeCopySession.instances = []


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "rss_settings.json"

    def write(section: dict | None) -> None:
        path.write_text(json.dumps({"browser_backend": section} if section is not None else {}), encoding="utf-8")

    monkeypatch.setattr(rss_config, "SETTINGS_PATH", path)
    return write


@pytest.fixture
def fake_bsk(tmp_path, monkeypatch):
    """假 bsk CLI + 记录每次子命令调用；BskBrowser 不传 command，靠 patch 解析入口注入。"""
    script = tmp_path / "fake_bsk.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    log_path = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_BSK_SCENARIO", str(scenario_path))
    monkeypatch.setattr("connectors._shared.bsk_browser.resolve_bsk_command", lambda: (sys.executable, str(script)))

    def configure(**scenario) -> None:
        scenario["log_path"] = str(log_path)
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")

    def calls() -> list[dict]:
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    configure(evaluate={"location.href": {"ok": True, "value": ALPHAPAI_URL}, ".length": {"ok": True, "value": 3}})
    return SimpleNamespace(configure=configure, calls=calls)


def _ok_result(source: dict, entries: int = 1) -> FeedFetchResult:
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=source["feed_url"],
        ok=True,
        status=200,
        entries=[FeedEntry(source["id"], source["name"], f"标题{i}", ALPHAPAI_URL, "2026-09-22", "摘要") for i in range(entries)],
    )


# ---- 后端选择 ----


def test_default_backend_is_copy_and_never_touches_bsk(settings_file, fake_bsk, monkeypatch):
    settings_file(None)  # 完全没有 browser_backend 配置项
    sessions: list = []
    monkeypatch.setattr(web_fetch, "_make_copy_session_for", FakeCopySession)
    monkeypatch.setattr(
        web_fetch, "_fetch_one_via_session", lambda session, source, **kw: sessions.append(session) or _ok_result(source)
    )

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert result.ok
    assert sessions and all(isinstance(s, FakeCopySession) for s in sessions)
    assert fake_bsk.calls() == []  # 一个 bsk 子进程都没起
    assert FakeCopySession.instances[0].shutdown_calls == 1


def test_bsk_backend_prefers_bsk_and_drives_page_ops(settings_file, fake_bsk, monkeypatch):
    settings_file({"default": "copy", "sources": {"alphapai": "bsk"}})
    seen: list = []

    def fake_fetch(page, source, timeout_ms=0, limit=0):
        seen.append((str(page.url), page.locator(".list-box").count()))
        return _ok_result(source)

    monkeypatch.setattr(web_fetch, "fetch_alphapai_with_page", fake_fetch)

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert result.ok
    assert seen == [(ALPHAPAI_URL, 3)]  # 页面操作真的落到了 bsk 上
    subcommands = [c["argv"][0] for c in fake_bsk.calls()]
    assert "evaluate" in subcommands and "session" in subcommands


def test_per_source_override_wins_over_default(settings_file):
    settings_file({"default": "bsk", "sources": {"alphapai": "copy", "youtube": "bsk"}})
    assert rss_config.browser_backend_for(SOURCE) == "copy"
    assert rss_config.browser_backend_for({"id": "youtube"}) == "bsk"
    assert rss_config.browser_backend_for({"id": "x"}) == "bsk"


def test_invalid_backend_value_falls_back_to_copy(settings_file):
    settings_file({"default": "selenium", "sources": {"alphapai": "wat"}})
    assert rss_config.browser_backend_for(SOURCE) == "copy"


def test_timeout_values_are_bounded(settings_file):
    settings_file({"request_help_timeout": -1, "command_timeout": "abc"})
    low, high = rss_config.BROWSER_TIMEOUT_BOUNDS
    assert rss_config.browser_request_help_timeout() == low  # 过小值夹到下限
    assert rss_config.browser_command_timeout() == rss_config.DEFAULT_COMMAND_TIMEOUT_SECONDS  # 非法值回落默认
    settings_file({"command_timeout": 99999})
    assert rss_config.browser_command_timeout() == high


# ---- 回退 ----


def test_bsk_failure_falls_back_to_copy_exactly_once(settings_file, fake_bsk, monkeypatch):
    settings_file({"sources": {"alphapai": "bsk"}})
    sessions: list = []

    def fake_fetch_once(session, source, **kwargs):
        sessions.append(session)
        if isinstance(session, BskRunnerSession):
            raise BskCommandError("navigate 失败 (exit=1, code=not_found)")
        return _ok_result(source)

    monkeypatch.setattr(web_fetch, "_make_copy_session_for", FakeCopySession)
    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_fetch_once)

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert result.ok
    assert len(sessions) == 2
    assert isinstance(sessions[0], BskRunnerSession) and isinstance(sessions[1], FakeCopySession)
    assert FakeCopySession.instances[0].shutdown_calls == 1
    # bsk 会话被结束（不留半开 session）
    assert any(c["argv"][0:2] == ["session", "stop"] for c in fake_bsk.calls())


def test_bsk_failure_with_failing_fallback_reports_copy_profile_path(settings_file, fake_bsk, monkeypatch):
    settings_file({"sources": {"alphapai": "bsk"}})

    def fake_fetch_once(session, source, **kwargs):
        if isinstance(session, BskRunnerSession):
            raise BskCommandError("session start 失败")
        return FeedFetchResult(source["id"], source["name"], source["feed_url"], False, 0, [], "登录态失效")

    monkeypatch.setattr(web_fetch, "_make_copy_session_for", FakeCopySession)
    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_fetch_once)

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert not result.ok
    assert "bsk 后端失败" in result.error
    assert "已回退到复制 profile 路径" in result.error
    assert "登录态失效" in result.error


def test_non_ok_bsk_result_also_falls_back(settings_file, fake_bsk, monkeypatch):
    """feed.py 会把页面异常吞成非 ok 结果，回退不能只认异常。"""
    settings_file({"sources": {"alphapai": "bsk"}})

    def fake_fetch_once(session, source, **kwargs):
        if isinstance(session, BskRunnerSession):
            return FeedFetchResult(source["id"], source["name"], source["feed_url"], False, 0, [], "蓝宝书页面未完成加载")
        return _ok_result(source)

    monkeypatch.setattr(web_fetch, "_make_copy_session_for", FakeCopySession)
    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_fetch_once)

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert result.ok
    assert len(FakeCopySession.instances) == 1


def test_fallback_saves_entries_only_once(settings_file, fake_bsk, monkeypatch):
    """回退只返回一个结果，所以同一个源只落一次库。"""
    settings_file({"sources": {"alphapai": "bsk"}})
    saved: list = []

    def fake_run_once(session, source, *, limit, timeout_ms):
        if isinstance(session, BskRunnerSession):
            raise BskCommandError("evaluate 超时")
        return _ok_result(source, entries=2)

    monkeypatch.setattr(alphapai_runner, "_run_fetch_once", fake_run_once)
    monkeypatch.setattr(alphapai_runner, "_make_session", FakeCopySession)
    monkeypatch.setattr(alphapai_runner, "save_entries", lambda entries: saved.append(list(entries)) or len(list(entries)))

    result, inserted = alphapai_runner.fetch_and_save_alphapai(SOURCE)

    assert result.ok and inserted == 2
    assert len(saved) == 1 and len(saved[0]) == 2


def test_explicit_session_argument_skips_backend_selection(settings_file, fake_bsk, monkeypatch):
    """调用方显式注入会话时沿用原逻辑（不切 bsk）。"""
    settings_file({"sources": {"alphapai": "bsk"}})
    seen: list = []

    def fake_run_once(session, source, *, limit, timeout_ms):
        seen.append(session)
        return _ok_result(source)

    monkeypatch.setattr(alphapai_runner, "_run_fetch_once", fake_run_once)
    session = FakeCopySession()

    result = alphapai_runner.fetch_alphapai_source(SOURCE, session=session)

    assert result.ok
    assert seen == [session]
    assert fake_bsk.calls() == []


# ---- 超时取值 ----


def test_slow_command_timeout_is_much_larger_than_help_watchdog(settings_file):
    settings_file({"request_help_timeout": 30, "command_timeout": 120})
    session = BskRunnerSession(start_url="about:blank")
    browser = session.browser
    assert browser._request_help_timeout == 30
    assert browser._slow_command_timeout == 120
    # 子进程硬超时留出余量，让 bsk 自身的 --timeout 先到期（否则会被我们 kill，丢掉错误分类）
    assert browser._timeout > browser._slow_command_timeout
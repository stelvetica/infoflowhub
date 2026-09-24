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
from connectors._shared.bsk_browser import BskCommandError, BskError
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


# ---- bsk 失败：重试配置次数后返回失败结果，绝不自动回退 copy ----


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("connectors._shared.bsk_session.BSK_RETRY_DELAY_SECONDS", 0.0)


def test_bsk_failure_retries_configured_times_and_never_builds_copy(settings_file, fake_bsk, monkeypatch):
    settings_file({"sources": {"alphapai": "bsk"}, "bsk_attempts": 3})
    seen: list = []

    def fake_fetch_once(session, source, **kwargs):
        seen.append(session)
        raise BskCommandError("navigate 失败 (exit=1, code=not_found)")

    def boom(*args, **kwargs):
        raise AssertionError("bsk 失败不得构造 copy 会话")

    monkeypatch.setattr(web_fetch, "_make_copy_session_for", boom)
    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_fetch_once)

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert not result.ok
    assert len(seen) == 3 and all(isinstance(s, BskRunnerSession) for s in seen)
    assert FakeCopySession.instances == []  # 全程零 copy 会话构造
    assert "已尝试 3 次，未回退 copy" in result.error
    # bsk 会话每次都被结束（不留半开 session）
    assert any(c["argv"][0:2] == ["session", "stop"] for c in fake_bsk.calls())


def test_bsk_failure_error_reports_no_copy_fallback(settings_file, fake_bsk, monkeypatch):
    settings_file({"sources": {"alphapai": "bsk"}})

    def fake_fetch_once(session, source, **kwargs):
        raise BskCommandError("session start 失败")

    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_fetch_once)

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert not result.ok
    assert "bsk 后端失败" in result.error
    assert "已尝试 2 次，未回退 copy" in result.error
    assert "session start 失败" in result.error
    assert "已回退到复制 profile 路径" not in result.error


def test_non_ok_bsk_result_also_retries_and_reports_no_copy(settings_file, fake_bsk, monkeypatch):
    """feed.py 会把页面异常吞成非 ok 结果，重试判定不能只认异常。"""
    settings_file({"sources": {"alphapai": "bsk"}})
    attempts: list = []

    def fake_fetch_once(session, source, **kwargs):
        attempts.append(session)
        return FeedFetchResult(source["id"], source["name"], source["feed_url"], False, 0, [], "蓝宝书页面未完成加载")

    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_fetch_once)

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert not result.ok
    assert len(attempts) == 2  # 非 ok 结果同样重试到配置次数
    assert "蓝宝书页面未完成加载" in result.error and "未回退 copy" in result.error
    assert FakeCopySession.instances == []


def test_bsk_failure_saves_no_entries(settings_file, fake_bsk, monkeypatch):
    """bsk 失败不再回退 copy，所以没有任何结果可落库。"""
    settings_file({"sources": {"alphapai": "bsk"}})
    saved: list = []

    def fake_run_once(session, source, *, limit, timeout_ms):
        raise BskCommandError("evaluate 超时")

    def boom():
        raise AssertionError("bsk 失败不得回退 copy 会话")

    monkeypatch.setattr(alphapai_runner, "_run_fetch_once", fake_run_once)
    monkeypatch.setattr(alphapai_runner, "_make_session", boom)
    monkeypatch.setattr(alphapai_runner, "save_entries", lambda entries: saved.append(list(entries)) or len(list(entries)))

    result, inserted = alphapai_runner.fetch_and_save_alphapai(SOURCE)

    assert not result.ok and inserted == 0
    assert saved == []
    assert "未回退 copy" in result.error


# ---- bsk_attempts 配置 ----


def test_bsk_attempts_invalid_value_falls_back_and_clamps(settings_file):
    settings_file(None)
    assert rss_config.bsk_attempts() == rss_config.DEFAULT_BSK_ATTEMPTS
    settings_file({"bsk_attempts": "abc"})
    assert rss_config.bsk_attempts() == rss_config.DEFAULT_BSK_ATTEMPTS
    settings_file({"bsk_attempts": -5})
    assert rss_config.bsk_attempts() == rss_config.BSK_ATTEMPTS_BOUNDS[0]
    settings_file({"bsk_attempts": 999})
    assert rss_config.bsk_attempts() == rss_config.BSK_ATTEMPTS_BOUNDS[1]


# ---- 失败必须可见：写进源健康与全局状态 ----


def test_bsk_failure_error_is_written_to_source_health(monkeypatch, tmp_path):
    from apps.subscriptions import runtime_health

    monkeypatch.setattr(runtime_health, "HEALTH_PATH", tmp_path / "health.json")
    failure = FeedFetchResult(
        SOURCE["id"], SOURCE["name"], SOURCE["feed_url"], False, 0, [],
        "bsk 后端失败（已尝试 2 次，未回退 copy）：BskCommandError: navigate 失败",
    )

    runtime_health.update_source_health(failure)

    saved = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert "未回退 copy" in saved["sources"]["alphapai"]["last_error"]


def test_bsk_failure_surfaces_in_run_outcome_and_status(monkeypatch, tmp_path):
    from apps.subscriptions import runtime_health

    monkeypatch.setattr(runtime_health, "HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(runtime_health, "STATUS_PATH", tmp_path / "status.json")
    failure = FeedFetchResult(
        SOURCE["id"], SOURCE["name"], SOURCE["feed_url"], False, 0, [],
        "bsk 后端失败（已尝试 2 次，未回退 copy）：boom",
    )
    monkeypatch.setattr(runtime_health, "fetch_many", lambda *a, **k: [failure])

    outcome = runtime_health.run_source_fetch([SOURCE], persist_global=True)

    assert outcome.failures and "未回退 copy" in outcome.failures[0]
    assert "未回退 copy" in outcome.status["last_error"]


# ---- 显式 copy 入口（设置页「用 copy 重试」专用） ----


def test_fetch_source_via_copy_runs_copy_path_once(settings_file, monkeypatch):
    sessions: list = []
    monkeypatch.setattr(web_fetch, "_make_copy_session_for", FakeCopySession)
    monkeypatch.setattr(
        web_fetch, "_fetch_one_via_session", lambda session, source, **kw: sessions.append(session) or _ok_result(source)
    )

    result = web_fetch.fetch_source_via_copy(SOURCE)

    assert result.ok
    assert len(sessions) == 1 and isinstance(sessions[0], FakeCopySession)
    assert FakeCopySession.instances[0].shutdown_calls == 1


def test_copy_retry_available_only_for_bsk_failure():
    from web.services.views import copy_retry_available

    assert copy_retry_available("") is False
    assert copy_retry_available("网络错误") is False
    assert copy_retry_available("bsk 后端失败（已尝试 2 次，未回退 copy）：boom") is True


# ---- 设置页按钮与 copy-retry 路由 ----


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.routes import main as main_routes

    app = FastAPI()
    app.include_router(main_routes.router)
    return TestClient(app)


def _settings_with(rows: list[dict]) -> dict:
    return {"sources": rows, "q": "", "source_filter": "", "sort": "name", "dir": "asc"}


def test_copy_retry_button_hidden_without_bsk_failure(monkeypatch):
    from web.routes import main as main_routes

    row = {"id": "alphapai", "name": "蓝宝书", "enabled": True, "last_error": "网络错误", "copy_retry_available": False}
    monkeypatch.setattr(main_routes, "get_settings_view", lambda params: _settings_with([row]))

    response = _client().get("/fragments/sources")

    assert response.status_code == 200
    assert "/copy-retry" not in response.text


def test_copy_retry_button_shown_with_bsk_failure(monkeypatch):
    from web.routes import main as main_routes

    row = {
        "id": "alphapai",
        "name": "蓝宝书",
        "enabled": True,
        "last_error": "bsk 后端失败（已尝试 2 次，未回退 copy）：boom",
        "copy_retry_available": True,
    }
    monkeypatch.setattr(main_routes, "get_settings_view", lambda params: _settings_with([row]))

    response = _client().get("/fragments/sources")

    assert response.status_code == 200
    assert "/actions/source/alphapai/copy-retry" in response.text
    assert "用 copy 重试" in response.text


def test_copy_retry_post_runs_copy_once_and_saves_once(monkeypatch):
    from apps.subscriptions import rss_db, runtime_health
    from web.routes import main as main_routes

    monkeypatch.setattr(main_routes, "get_settings_view", lambda params: _settings_with([]))
    monkeypatch.setattr(main_routes, "normalize_sources", lambda: [SOURCE])
    calls = {"fetch": 0, "health": 0, "saved": []}

    def fake_fetch(source, **kwargs):
        calls["fetch"] += 1
        return _ok_result(source, entries=2)

    monkeypatch.setattr("connectors._shared.web_fetch.fetch_source_via_copy", fake_fetch)
    monkeypatch.setattr(runtime_health, "update_source_health", lambda result: calls.__setitem__("health", calls["health"] + 1))
    monkeypatch.setattr(rss_db, "save_entries", lambda entries: calls["saved"].append(list(entries)) or len(list(entries)))

    response = _client().post("/actions/source/alphapai/copy-retry")

    assert response.status_code == 200
    assert calls["fetch"] == 1
    assert calls["health"] == 1
    assert len(calls["saved"]) == 1 and len(calls["saved"][0]) == 2  # 只落一次库


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


# ---- bsk 会话构造失败不得炸穿整批（H1） ----


def test_bsk_session_construction_failure_does_not_break_batch(settings_file, fake_bsk, monkeypatch):
    """bsk CLI 未安装 / rss_settings.json 损坏等构造期异常，只作本源失败结果，
    同批其他源已抓好的结果不得被丢弃（历史上曾整批丢失）。"""
    settings_file({"sources": {"alphapai": "bsk"}})

    def boom():
        raise BskError("未找到 bsk CLI，请先安装 browser-skill（bsk）")

    monkeypatch.setattr(alphapai_runner, "_make_bsk_session", boom)

    from connectors.rss import fetch as rss_fetch

    other = {"id": "some-rss", "name": "某 RSS", "feed_url": "https://example.com/feed", "provider": "rss"}
    monkeypatch.setattr(
        rss_fetch,
        "fetch_feed",
        lambda source_id, source_name, feed_url, timeout=20, limit=60: FeedFetchResult(
            source_id, source_name, feed_url, True, 200, [], ""
        ),
    )

    results = rss_fetch.fetch_many([SOURCE, other])

    by_id = {result.source_id: result for result in results}
    assert len(results) == 2
    assert by_id["alphapai"].ok is False
    assert "bsk 会话构造失败" in by_id["alphapai"].error
    assert by_id["some-rss"].ok is True  # 同批其他源不受影响
    assert fake_bsk.calls() == []  # 构造都没成，一个 bsk 子进程都没起


def test_broken_settings_does_not_raise_out_of_alphapai_source(tmp_path, monkeypatch):
    """rss_settings.json 损坏时后端判定本身会抛；bsk 分支也不得把异常抛给调用方。"""
    broken = tmp_path / "rss_settings.json"
    broken.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(rss_config, "SETTINGS_PATH", broken)

    result = alphapai_runner.fetch_alphapai_source(SOURCE)

    assert not result.ok
    assert result.entries == []


# ---- 超时取值 ----


def test_slow_command_timeout_is_much_larger_than_help_watchdog(settings_file):
    settings_file({"request_help_timeout": 30, "command_timeout": 120})
    session = BskRunnerSession(start_url="about:blank")
    browser = session.browser
    assert browser._request_help_timeout == 30
    assert browser._slow_command_timeout == 120
    # 子进程硬超时留出余量，让 bsk 自身的 --timeout 先到期（否则会被我们 kill，丢掉错误分类）
    assert browser._timeout > browser._slow_command_timeout
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


# ---- 后端固定 bsk（copy 已整体禁用） ----


def test_copy_backend_switch_is_gone(settings_file):
    """不存在可切换到 copy 的配置入口：浏览器抓取只走 bsk。"""
    settings_file({"default": "copy", "sources": {"alphapai": "copy"}})
    assert not hasattr(rss_config, "browser_backend_for")
    assert not hasattr(web_fetch, "fetch_source_via_copy")
    assert not hasattr(web_fetch, "_make_copy_session_for")


def test_bsk_backend_prefers_bsk_and_drives_page_ops(settings_file, fake_bsk, monkeypatch):
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

    monkeypatch.setattr(web_fetch, "_fetch_one_via_session", fake_fetch_once)

    result = web_fetch._fetch_one_with_backend(SOURCE)

    assert not result.ok
    assert len(seen) == 3 and all(isinstance(s, BskRunnerSession) for s in seen)
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


def test_bsk_failure_saves_no_entries(settings_file, fake_bsk, monkeypatch):
    """bsk 失败没有任何结果可落库。"""
    settings_file({"sources": {"alphapai": "bsk"}})
    saved: list = []

    def fake_run_once(session, source, *, limit, timeout_ms):
        raise BskCommandError("evaluate 超时")

    monkeypatch.setattr(alphapai_runner, "_run_fetch_once", fake_run_once)
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


# ---- copy 入口与路由已移除 ----


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.routes import main as main_routes

    app = FastAPI()
    app.include_router(main_routes.router)
    return TestClient(app)


def test_copy_retry_route_is_gone(monkeypatch):
    from web.routes import main as main_routes

    monkeypatch.setattr(main_routes, "get_settings_view", lambda params: {"sources": [], "q": "", "source_filter": "", "sort": "name", "dir": "asc"})
    response = _client().post("/actions/source/alphapai/copy-retry")

    assert response.status_code == 404


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

# ---- 浏览器源一律 bsk 守卫 ----


def test_every_enabled_browser_source_runs_on_bsk(monkeypatch):
    """所有走浏览器的启用源都只能构造 bsk 会话——没有任何配置能改回 copy。"""
    from connectors._shared.common import resolve_web_target
    from connectors._shared.bsk_session import BskRunnerSession

    sources = json.loads(rss_config.SOURCES_PATH.read_text(encoding="utf-8"))["sources"]
    browser_path = []
    for source in sources:
        if not source.get("enabled"):
            continue
        target = resolve_web_target(source)
        site = target.site if target else None
        # alphapai 按 id 特判；其余浏览器源以站点识别为准
        if source.get("id") == "alphapai" or site in web_fetch._SHARED_RUNNER_SITES or site == "bilibili":
            browser_path.append((source, site))
            assert isinstance(web_fetch._make_session_for(source), BskRunnerSession)

    # 守卫本身要有效：确实存在走浏览器的源
    assert browser_path, "未识别到任何浏览器源，守卫失效"

"""7:10 前置抓取槽与交易日门禁的窗口/状态语义回归检查。

覆盖不变式：窗口外不跑、窗口内当天跑一次、过 8:10 不补跑、
前置槽不得写 last_run_date（否则 8:10 晨间流程会被判「今天已跑」而整天不跑）、
非交易日两个槽都不动作且落盘 market_gate、门禁开关关掉后行为等同改动前。
全程打桩：不抓真实数据、不碰真实 runtime 状态文件、不查真实交易日历、不起交易日历刷新子进程。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from infra.market_calendar import MarketVerdict
from web.services import auto_runner, views
from web.services.auto_runner import AutoRunner

CLOSED = MarketVerdict("A", "2026-10-07", "closed", "eastmoney-kline", "日K不含今日", "2026-10-07 08:20:00")
TRADING = MarketVerdict("A", "2026-10-07", "trading", "bluebook", "晨会版已刊出", "2026-10-07 08:20:00")


class _FakeDatetime(datetime):
    """:meth:`datetime.now` 打桩；其余行为沿用真 datetime（strftime/date 等）。"""

    fixed: datetime = datetime(2026, 10, 7, 7, 10)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return cls.fixed


@pytest.fixture()
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AutoRunner, list[str]]:
    monkeypatch.setattr(views, "AUTOMATION_RUNTIME_PATH", tmp_path / "automation_runtime.json")
    monkeypatch.setattr(auto_runner, "datetime", _FakeDatetime)
    monkeypatch.delenv(auto_runner.MARKET_GATE_FLAG, raising=False)
    monkeypatch.delenv(auto_runner.GATE_NETWORK_FLAG, raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        auto_runner,
        "_fetch_all",
        lambda: calls.append("fetch") or {"subscriptions": {}, "laterhub": {}},
    )
    # 默认「今天是交易日」：既有用例的行为不变式不该依赖真实日历语料。
    monkeypatch.setattr(
        auto_runner, "market_gate", lambda *a, **kw: TRADING, raising=True
    )
    # 晨间流程会跑真实 skill / 开浏览器，这里全部短路，只管调度语义。
    monkeypatch.setattr(auto_runner, "run_skill", lambda name, cwd=None: {"name": name, "status": "success"})
    monkeypatch.setattr(auto_runner, "publish_reports", lambda: None)
    monkeypatch.setattr(auto_runner, "_open_panel", lambda: None)
    # 交易日历刷新会真起 node 子进程（跨仓、带网络）：默认打桩成「不动作」，
    # 否则测试行为会随「今天几号 / 本机日历是否可用」漂移。专门用例再各自覆盖。
    monkeypatch.setattr(auto_runner, "refresh_local_calendar", lambda now=None: "")
    return AutoRunner(), calls


def _tick(runner: AutoRunner) -> None:
    asyncio.run(runner._maybe_run_prefetch())


def _morning(runner: AutoRunner) -> None:
    asyncio.run(runner._maybe_run_morning())


def _set_now(hour: int, minute: int) -> None:
    _FakeDatetime.fixed = datetime(2026, 10, 7, hour, minute)


def test_skips_before_window(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(6, 59)
    _tick(bot)
    assert calls == []


def test_runs_once_inside_window(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(7, 10)
    _tick(bot)
    assert calls == ["fetch"]
    slot = bot._slot("prefetch")
    assert slot["status"] == "success"
    assert slot["last_attempt_date"] == "2026-10-07"

    _set_now(7, 40)  # 同一天再 tick 不得重复抓
    _tick(bot)
    assert calls == ["fetch"]


def test_does_not_consume_morning_slot(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, _calls = runner
    _set_now(7, 10)
    _tick(bot)
    # last_run_date 是晨间流程的槽位标记，前置槽绝不能写——否则 8:10 流程整天不跑
    assert bot._load_state().get("last_run_date") in ("", None)


def test_skips_after_morning_start(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(8, 30)  # 机器起晚了：晨间流程自己会抓，前置槽不补跑
    _tick(bot)
    assert calls == []


# ── 交易日历刷新挂在 7:10 前置槽上 ───────────────────────────────


def test_prefetch_refreshes_calendar_once_and_still_fetches(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """刷新点复用前置槽的「当天一次」语义：窗口内只刷一遍，且不阻断抓取。"""
    bot, calls = runner
    seen: list[Any] = []
    monkeypatch.setattr(
        auto_runner, "refresh_local_calendar", lambda now=None: seen.append(now) or "本地交易日历已刷新"
    )
    _set_now(7, 10)
    _tick(bot)
    assert seen == [datetime(2026, 10, 7, 7, 10)]  # 用槽里同一个 now，不重新取时钟
    assert calls == ["fetch"]

    _set_now(7, 40)  # 窗口内再 tick：槽守卫挡住，刷新跟着只跑一遍
    _tick(bot)
    assert len(seen) == 1
    assert calls == ["fetch"]


def test_prefetch_does_not_refresh_outside_window(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _calls = runner
    seen: list[Any] = []
    monkeypatch.setattr(auto_runner, "refresh_local_calendar", lambda now=None: seen.append(now) or "")
    _set_now(6, 59)
    _tick(bot)
    _set_now(8, 30)
    _tick(bot)
    assert seen == []


def test_prefetch_logs_refresh_note(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot, _calls = runner
    monkeypatch.setattr(auto_runner, "refresh_local_calendar", lambda now=None: "本地交易日历已刷新")
    with caplog.at_level(logging.INFO, logger="web.services.auto_runner"):
        _set_now(7, 10)
        _tick(bot)
    assert any("本地交易日历已刷新" in record.getMessage() for record in caplog.records)


def test_prefetch_is_silent_when_refresh_returns_nothing(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """返回空串 = 不动作（非 10/20 且日历未过期）：不写日志，也没有其它副作用。"""
    bot, calls = runner
    monkeypatch.setattr(auto_runner, "refresh_local_calendar", lambda now=None: "")
    with caplog.at_level(logging.INFO, logger="web.services.auto_runner"):
        _set_now(7, 10)
        _tick(bot)
    assert not any("交易日历" in record.getMessage() for record in caplog.records)
    assert calls == ["fetch"]
    assert bot._slot("prefetch")["last_attempt_date"] == "2026-10-07"


def test_failure_not_retried_same_day(runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _calls = runner
    attempts: list[str] = []

    def _boom() -> dict:
        attempts.append("boom")
        raise RuntimeError("网络抖动")

    monkeypatch.setattr(auto_runner, "_fetch_all", _boom)
    _set_now(7, 10)
    _tick(bot)
    assert bot._slot("prefetch")["status"] == "error"

    _set_now(7, 11)  # 窗口内下一 tick 不得重试（否则每 60 秒白抓一次）
    _tick(bot)
    assert attempts == ["boom"]


def test_non_trading_day_skips_morning_but_prefetch_still_runs(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, calls = runner
    monkeypatch.setattr(auto_runner, "market_gate", lambda *a, **kw: CLOSED)

    _set_now(7, 10)
    _tick(bot)
    # 前置槽不过门禁：它是给早读备料的，周末与节假日照样要看全球版/韭研
    assert calls == ["fetch"]
    assert bot._slot("prefetch")["last_attempt_date"] == "2026-10-07"

    _set_now(8, 30)
    _morning(bot)
    assert calls == ["fetch"]  # 晨间流程不动作
    state = bot._load_state()
    assert state["market_gate"]["verdict"] == "closed"
    assert state["market_gate"]["source"] == "eastmoney-kline"
    # 未真跑，不得占用晨间槽位
    assert state.get("last_run_date") in ("", None)
    assert state.get("flow") is None


def test_closed_verdict_short_circuits_later_ticks(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, calls = runner
    probes: list[str] = []

    def gate(*a: Any, **kw: Any) -> MarketVerdict:
        probes.append("gate")
        return CLOSED

    monkeypatch.setattr(auto_runner, "market_gate", gate)
    _set_now(8, 30)
    _morning(bot)
    _set_now(8, 31)
    _morning(bot)
    _set_now(9, 0)
    _morning(bot)
    # 决定性结论缓存到当天结束：后续 tick 直接命中缓存，不再重算
    assert probes == ["gate"]
    assert calls == []


def test_closed_verdict_from_disk_survives_restart(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, calls = runner

    def _explode(*a: Any, **kw: Any) -> MarketVerdict:
        raise AssertionError("落盘已有闭市结论，重启后不该再查一次")

    # 落盘一份今晨的闭市结论（模拟上次进程留下的状态）
    state = bot._load_state()
    state["market_gate"] = dict(CLOSED.to_state())
    bot._save_state(state)

    fresh = AutoRunner()  # 新进程：内存缓存为空
    monkeypatch.setattr(auto_runner, "market_gate", _explode)
    _set_now(8, 30)
    asyncio.run(fresh._maybe_run_morning())
    assert calls == []


def test_trading_day_keeps_current_behaviour(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(7, 10)
    _tick(bot)
    assert calls == ["fetch"]

    _set_now(8, 30)
    _morning(bot)
    assert calls == ["fetch", "fetch"]  # 晨间流程自己再抓一次
    state = bot._load_state()
    assert state["last_run_date"] == "2026-10-07"
    assert state["flow"]["status"] in ("success", "partial")


def test_gate_flag_off_restores_old_behaviour(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, calls = runner
    monkeypatch.setenv(auto_runner.MARKET_GATE_FLAG, "0")

    def _explode(*a: Any, **kw: Any) -> MarketVerdict:
        raise AssertionError("门禁关闭时不该查证据")

    monkeypatch.setattr(auto_runner, "market_gate", _explode)

    _set_now(7, 10)
    _tick(bot)
    _set_now(8, 30)
    _morning(bot)
    assert calls == ["fetch", "fetch"]
    assert "market_gate" not in bot._load_state()  # 关闭时也不落盘


def test_network_flag_off_is_passed_through(runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _calls = runner
    seen: list[bool] = []

    def gate(market, *, now=None, allow_network=True, bluebook_dir=None):  # noqa: ANN001, ANN202
        seen.append(allow_network)
        return TRADING

    monkeypatch.setenv(auto_runner.GATE_NETWORK_FLAG, "0")
    monkeypatch.setattr(auto_runner, "market_gate", gate)
    # 门禁只在晨间流程取值（前置槽不过门禁），故用晨间路径验网络开关透传
    _set_now(8, 30)
    _morning(bot)
    assert seen == [False]


def test_run_skills_now_bypasses_gate(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _calls = runner
    monkeypatch.setattr(auto_runner, "market_gate", lambda *a, **kw: CLOSED)
    skills = asyncio.run(bot.run_skills_now())
    # 手动触发即视为覆盖：门禁不拦手动补跑
    assert [s["name"] for s in skills] == list(auto_runner.SKILLS)

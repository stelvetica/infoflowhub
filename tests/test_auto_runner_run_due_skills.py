"""新闻公告面板「跑技能」按钮的回归检查。

覆盖不变式：非交易日不跑；交易日跑公告+新闻；本周最后一个交易日再加操作分析；
操作分析当天已跑过就不重复；连点/流程运行中不叠加；异常把 flow 落成 error 并释放占位。
全程打桩：不跑真实 skill、不碰真实 runtime 状态文件、不读真实交易日历。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from web.services import auto_runner, views
from web.services.auto_runner import SKILLS, AutoRunner

DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"]


class _FakeDatetime(datetime):
    fixed: datetime = datetime(2026, 11, 5, 9, 0)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return cls.fixed


@pytest.fixture()
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AutoRunner, list[str]]:
    monkeypatch.setattr(views, "AUTOMATION_RUNTIME_PATH", tmp_path / "automation_runtime.json")
    monkeypatch.setattr(auto_runner, "datetime", _FakeDatetime)
    monkeypatch.setattr(auto_runner, "local_trading_days", lambda: list(DAYS))
    monkeypatch.setattr(auto_runner, "publish_reports", lambda: None)
    monkeypatch.setattr(auto_runner, "_open_panel", lambda: None)
    monkeypatch.setattr(auto_runner, "_flow_products", lambda started_at: [{"name": "x", "kind": "k", "is_image": False}])
    calls: list[str] = []

    def fake_run_skill(name: str, *, cwd=None, timeout=None, **_: object) -> dict:
        calls.append(name)
        return {"name": name, "status": "success", "error": ""}

    monkeypatch.setattr(auto_runner, "run_skill", fake_run_skill)
    _FakeDatetime.fixed = datetime(2026, 11, 5, 9, 0)
    return AutoRunner(), calls


def _verdict(value: str):
    return lambda self, now: {"verdict": value, "source": "test", "date": now.date().isoformat()}


def _set_now(day: int, hour: int, minute: int) -> None:
    _FakeDatetime.fixed = datetime(2026, 11, day, hour, minute)


def test_due_on_trading_day_is_announcements_and_news(runner: tuple[AutoRunner, list[str]], monkeypatch) -> None:
    bot, _ = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("trading"))
    _set_now(5, 9, 0)  # 周四：本周还有周五，操作分析不到期
    assert bot.due_skill_names() == list(SKILLS)


def test_due_includes_operations_on_last_trading_day(runner: tuple[AutoRunner, list[str]], monkeypatch) -> None:
    bot, _ = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("trading"))
    _set_now(6, 9, 0)  # 周五是本周最后一个交易日
    assert bot.due_skill_names() == [*SKILLS, "portfolio-operations"]


def test_operations_not_repeated_when_already_ran_today(runner: tuple[AutoRunner, list[str]], monkeypatch) -> None:
    bot, _ = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("trading"))
    bot._write_slot("operations", {"last_attempt_date": "2026-11-06", "status": "success"})
    _set_now(6, 9, 0)
    assert bot.due_skill_names() == list(SKILLS)


def test_nothing_due_on_non_trading_day(runner: tuple[AutoRunner, list[str]], monkeypatch) -> None:
    bot, _ = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("closed"))
    _set_now(6, 9, 0)
    assert bot.due_skill_names() == []


def test_run_executes_chain_in_order_and_records_flow(runner: tuple[AutoRunner, list[str]], monkeypatch) -> None:
    bot, calls = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("trading"))
    _set_now(5, 9, 0)
    assert bot.claim_manual_run() is True
    skills = asyncio.run(bot.run_due_skills_now())
    assert calls == list(SKILLS)
    assert [item["name"] for item in skills] == list(SKILLS)
    state = bot._load_state()
    # 手动补跑不占晨间流程的 last_run_date，否则当天 8:10 的完整流程会被判「已跑」而跳过
    assert state.get("last_run_date") in (None, "")
    assert state["flow"]["status"] == "success"
    assert bot.claim_manual_run() is True  # 上一轮结束后可再次手动触发


def test_run_appends_operations_on_friday(runner: tuple[AutoRunner, list[str]], monkeypatch) -> None:
    bot, calls = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("trading"))
    _set_now(6, 9, 0)
    bot.claim_manual_run()
    skills = asyncio.run(bot.run_due_skills_now())
    assert calls == [*SKILLS, "portfolio-operations"]
    assert skills[-1]["name"] == "portfolio-operations"
    assert bot._slot("operations")["last_attempt_date"] == "2026-11-06"


def test_no_run_when_closed(runner: tuple[AutoRunner, list[str]], monkeypatch) -> None:
    bot, calls = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("closed"))
    _set_now(6, 9, 0)
    bot.claim_manual_run()
    assert asyncio.run(bot.run_due_skills_now()) == []
    assert calls == []
    assert bot._load_state().get("last_run_date") in (None, "")
    assert bot.claim_manual_run() is True  # 空跑也要释放占位


def test_claim_blocked_while_running(runner: tuple[AutoRunner, list[str]], monkeypatch) -> None:
    bot, _ = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("trading"))
    assert bot.claim_manual_run() is True
    assert bot.claim_manual_run() is False
    bot._manual_running = False
    bot._begin_flow(datetime(2026, 11, 5, 9, 0))
    assert bot.claim_manual_run() is False  # 晨间流程运行中不叠加


def test_exception_marks_flow_error_and_releases_claim(
    runner: tuple[AutoRunner, list[str]], monkeypatch
) -> None:
    bot, _ = runner
    monkeypatch.setattr(AutoRunner, "_market_verdict", _verdict("trading"))

    def failing(name: str, *, cwd=None, timeout=None, **_: object) -> dict:
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(auto_runner, "run_skill", failing)
    _set_now(5, 9, 0)
    bot.claim_manual_run()
    assert asyncio.run(bot.run_due_skills_now()) == []
    state = bot._load_state()
    assert state["flow"]["status"] == "error"
    assert "模型挂了" in state["last_error"]
    assert bot.claim_manual_run() is True

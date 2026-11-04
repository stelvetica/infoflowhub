"""每周最后一个交易日 7:30 的操作分析周任务回归检查。

覆盖不变式：只在「本周最后一个交易日」且 ≥7:30 触发、当天只跑一次、错过时刻当天补跑、
失败当天不重试（避免每 60 秒 tick 反复烧模型）、不占用晨间流程的 last_run_date 槽位。
全程打桩：不跑真实 skill、不碰真实 runtime 状态文件、不读真实交易日历。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from infra.market_calendar import is_last_trading_day_of_week
from web.services import auto_runner, views
from web.services.auto_runner import AutoRunner

# 2026-11-02 ~ 11-06 为完整交易周，11-09 为下周一
DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"]


class _FakeDatetime(datetime):
    """:meth:`datetime.now` 打桩；其余行为沿用真 datetime。"""

    fixed: datetime = datetime(2026, 11, 6, 7, 30)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return cls.fixed


@pytest.fixture()
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AutoRunner, list[str]]:
    monkeypatch.setattr(views, "AUTOMATION_RUNTIME_PATH", tmp_path / "automation_runtime.json")
    monkeypatch.setattr(auto_runner, "datetime", _FakeDatetime)
    monkeypatch.setattr(auto_runner, "local_trading_days", lambda: list(DAYS))
    calls: list[str] = []

    def fake_run_skill(name: str, *, cwd=None, timeout=None, **_: object) -> dict:
        calls.append(name)
        return {"name": name, "status": "success", "error": ""}

    monkeypatch.setattr(auto_runner, "run_skill", fake_run_skill)
    _FakeDatetime.fixed = datetime(2026, 11, 6, 7, 30)
    return AutoRunner(), calls


def _tick(bot: AutoRunner) -> None:
    asyncio.run(bot._maybe_run_operations())


def _set_now(day: int, hour: int, minute: int) -> None:
    _FakeDatetime.fixed = datetime(2026, 11, day, hour, minute)


def test_runs_at_730_on_last_trading_day(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(6, 7, 30)
    _tick(bot)
    assert calls == ["portfolio-operations"]
    slot = bot._slot("operations")
    assert slot["status"] == "success"
    assert slot["last_attempt_date"] == "2026-11-06"
    # 不得占用晨间流程的槽位，否则 8:10 会被判「今天已跑」
    assert bot._load_state().get("last_run_date") in (None, "")


def test_not_before_730(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(6, 7, 29)
    _tick(bot)
    assert calls == []


def test_not_on_non_last_trading_day(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(5, 10, 0)  # 周四，周五仍开市
    _tick(bot)
    assert calls == []


def test_catch_up_later_same_day(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(6, 9, 15)  # 7:30 时进程不在线，开机后补跑
    _tick(bot)
    assert calls == ["portfolio-operations"]


def test_once_per_day(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(6, 7, 30)
    _tick(bot)
    _set_now(6, 9, 0)
    _tick(bot)
    assert calls == ["portfolio-operations"]


def test_failure_not_retried_same_day(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, calls = runner

    def failing(name: str, *, cwd=None, timeout=None, **_: object) -> dict:
        calls.append(name)
        return {"name": name, "status": "error", "error": "模型挂了"}

    monkeypatch.setattr(auto_runner, "run_skill", failing)
    _set_now(6, 7, 30)
    _tick(bot)
    _set_now(6, 9, 0)
    _tick(bot)
    assert calls == ["portfolio-operations"]
    slot = bot._slot("operations")
    assert slot["status"] == "error"
    assert slot["error"] == "模型挂了"


def test_skips_holiday_friday(
    runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, calls = runner
    # 周五休市 → 日历不含 11-06，今天虽是周五也不该跑
    monkeypatch.setattr(auto_runner, "local_trading_days", lambda: [d for d in DAYS if d != "2026-11-06"])
    _set_now(6, 7, 30)
    _tick(bot)
    assert calls == []


@pytest.mark.parametrize(
    "day, expected",
    [
        (date(2026, 11, 6), True),   # 周五是本周最后交易日
        (date(2026, 11, 5), False),  # 周四，后面还有周五
        (date(2026, 11, 9), False),  # 下周一，后面还有周二
        (date(2026, 11, 7), False),  # 周六不在日历里
    ],
)
def test_last_trading_day_predicate(day: date, expected: bool) -> None:
    assert is_last_trading_day_of_week(day, DAYS) is expected


def test_last_trading_day_moves_up_when_friday_closed() -> None:
    days = [d for d in DAYS if d != "2026-11-06"]
    assert is_last_trading_day_of_week(date(2026, 11, 5), days) is True
    assert is_last_trading_day_of_week(date(2026, 11, 6), days) is False


def test_last_trading_day_falls_back_to_friday_without_calendar() -> None:
    assert is_last_trading_day_of_week(date(2026, 11, 6), []) is True
    assert is_last_trading_day_of_week(date(2026, 11, 5), []) is False

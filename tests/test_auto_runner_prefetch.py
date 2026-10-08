"""7:05 前置抓取槽的窗口与状态语义回归检查。

覆盖四条不变式：窗口外不跑、窗口内当天跑一次、过 8:10 不补跑、
前置槽不得写 last_run_date（否则 8:10 晨间流程会被判「今天已跑」而整天不跑）。
全程打桩：不抓真实数据、不碰真实 runtime 状态文件。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from web.services import auto_runner, views
from web.services.auto_runner import AutoRunner


class _FakeDatetime(datetime):
    """:meth:`datetime.now` 打桩；其余行为沿用真 datetime（strftime/date 等）。"""

    fixed: datetime = datetime(2026, 10, 7, 7, 5)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return cls.fixed


@pytest.fixture()
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AutoRunner, list[str]]:
    monkeypatch.setattr(views, "AUTOMATION_RUNTIME_PATH", tmp_path / "automation_runtime.json")
    monkeypatch.setattr(auto_runner, "datetime", _FakeDatetime)
    calls: list[str] = []
    monkeypatch.setattr(
        auto_runner,
        "_fetch_all",
        lambda: calls.append("fetch") or {"subscriptions": {}, "laterhub": {}},
    )
    return AutoRunner(), calls


def _tick(runner: AutoRunner) -> None:
    asyncio.run(runner._maybe_run_prefetch())


def _set_now(hour: int, minute: int) -> None:
    _FakeDatetime.fixed = datetime(2026, 10, 7, hour, minute)


def test_skips_before_window(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(6, 59)
    _tick(bot)
    assert calls == []


def test_runs_once_inside_window(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(7, 5)
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
    _set_now(7, 5)
    _tick(bot)
    # last_run_date 是晨间流程的槽位标记，前置槽绝不能写——否则 8:10 流程整天不跑
    assert bot._load_state().get("last_run_date") in ("", None)


def test_skips_after_morning_start(runner: tuple[AutoRunner, list[str]]) -> None:
    bot, calls = runner
    _set_now(8, 30)  # 机器起晚了：晨间流程自己会抓，前置槽不补跑
    _tick(bot)
    assert calls == []


def test_failure_not_retried_same_day(runner: tuple[AutoRunner, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _calls = runner
    attempts: list[str] = []

    def _boom() -> dict:
        attempts.append("boom")
        raise RuntimeError("网络抖动")

    monkeypatch.setattr(auto_runner, "_fetch_all", _boom)
    _set_now(7, 5)
    _tick(bot)
    assert bot._slot("prefetch")["status"] == "error"

    _set_now(7, 6)  # 窗口内下一 tick 不得重试（否则每 60 秒白抓一次）
    _tick(bot)
    assert attempts == ["boom"]

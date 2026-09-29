"""bsk daemon 自愈：开工前探活、不在就拉起、并把原因写成可读日志。

背景：daemon 闲置 10 分钟会自退（daemon 日志实测 `daemon exceeded idle threshold; exiting,
idle_secs=600`），而本模块所有命令都带 BSK_AUTO_START=0，所以它不在时 `session start` 只会
exit=2 让整源跳过——2026-09-29 08:12 的抖音/小黑盒就是这样被双双跳过的。

不触真实 daemon/Chrome：`_run` 被打桩成按 argv 分派的假实现。
"""

from __future__ import annotations

import json

import pytest

from connectors._shared import bsk_browser
from connectors._shared.bsk_browser import BskBrowser, BskResult, BskSessionError

READY = {"pid": 1, "uptime_secs": 3, "browsers": [{"instance_id": "b1"}]}
NO_BROWSER = {"pid": 1, "uptime_secs": 3, "browsers": []}
# 实测文案：daemon 不在且禁用了自动拉起时，bsk 自己的 status 输出
DAEMON_DOWN = {
    "code": None,
    "message": "ensure daemon is running: automatic daemon startup is disabled (BSK_AUTO_START=0)",
}


class FakeRun:
    """按 argv 分派的假 `_run`：status 依次返回预设结果，daemon start 可设为抛错。"""

    def __init__(self, statuses, *, daemon_error=None, session_result=None) -> None:
        self.statuses = list(statuses)
        self.daemon_error = daemon_error
        self.session_result = session_result if session_result is not None else (0, {"session_id": "s-1"})
        self.calls: list[list[str]] = []

    def _next_status(self):
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def __call__(
        self,
        args,
        *,
        label,
        timeout=None,
        check=True,
        stop_session_on_timeout=True,
        help_watchdog=True,
    ):
        argv = list(args)
        self.calls.append(argv)
        if argv[0] == "status":
            code, payload = self._next_status()
            return BskResult(returncode=code, stdout=json.dumps(payload) if payload is not None else "", stderr="")
        if argv[0] == "daemon":
            if self.daemon_error is not None:
                raise self.daemon_error
            return BskResult(returncode=0, stdout="daemon started", stderr="")
        if argv[0] == "session" and len(argv) > 1 and argv[1] == "start":
            code, payload = self.session_result
            return BskResult(returncode=code, stdout=json.dumps(payload), stderr="")
        return BskResult(returncode=0, stdout="", stderr="")


def _browser(fake: FakeRun, logs: list[str]) -> BskBrowser:
    browser = BskBrowser(command=("bsk",), logger=logs.append)
    browser._run = fake  # type: ignore[method-assign]
    return browser


def _kinds(calls: list[list[str]]) -> list[str]:
    return [c[0] for c in calls]


def test_daemon_down_is_started_then_session_runs(monkeypatch):
    """daemon 不在 → 自动拉起 → 会话照常建立（这条就是今早 08:12 缺失的自愈）。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    fake = FakeRun([(2, DAEMON_DOWN), (0, READY)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    assert browser.start() == "s-1"
    assert _kinds(fake.calls) == ["status", "daemon", "status", "session"]
    assert fake.calls[-1][0:3] == ["session", "start", "--json"]
    assert any("bsk daemon 不在" in line for line in logs)
    assert any("已拉起并就绪" in line for line in logs)


def test_daemon_ready_never_calls_daemon_start(monkeypatch):
    """daemon 就在跑时不该有多余动作（只探一次）。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    fake = FakeRun([(0, READY)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    browser.start()

    assert _kinds(fake.calls) == ["status", "session"]
    assert not any("尝试拉起" in line for line in logs)


def test_daemon_status_message_is_surfaced(monkeypatch):
    """daemon 自己给的原因（含权限不足这类）要出现在日志里，别只报 exit=2。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bsk_browser, "DAEMON_START_WAIT_SECONDS", 0.05)
    fake = FakeRun([(2, DAEMON_DOWN)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    browser.start()

    assert any("BSK_AUTO_START=0" in line for line in logs)
    assert any("仍未就绪" in line for line in logs)


def test_no_browser_connected_logs_actionable_hint(monkeypatch):
    """daemon 在但没连浏览器：给出可操作的提示（这才是需要人去开 Chrome 的场景）。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    fake = FakeRun([(0, NO_BROWSER)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    browser.start()

    assert any("未连接浏览器" in line and "Chrome" in line for line in logs)


def test_session_failure_includes_no_browser_hint(monkeypatch):
    """session start 失败时把「没连浏览器」写进错误里，排障不用去翻 daemon 日志。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    fake = FakeRun([(0, NO_BROWSER)], session_result=(2, {}))
    browser = _browser(fake, [])

    with pytest.raises(BskSessionError) as excinfo:
        browser.start()

    message = str(excinfo.value)
    assert "exit=2" in message
    assert "未连接任何浏览器" in message


def test_daemon_start_exception_does_not_block_session_start(monkeypatch):
    """拉起命令本身抛错也不能炸：继续走 session start，并把原因记下来。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    fake = FakeRun([(2, DAEMON_DOWN)], daemon_error=RuntimeError("bsk 不在 PATH"))
    logs: list[str] = []
    browser = _browser(fake, logs)

    assert browser.start() == "s-1"
    assert any("daemon start err" in line for line in logs)

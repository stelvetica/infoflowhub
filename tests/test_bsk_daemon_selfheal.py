"""bsk daemon / 浏览器自愈：开工前探活、daemon 不在就拉起、没连浏览器就拉起用户 Chrome。

背景：daemon 闲置 10 分钟会自退（daemon 日志实测 `daemon exceeded idle threshold; exiting,
idle_secs=600`），而本模块所有命令都带 BSK_AUTO_START=0，所以它不在时 `session start` 只会
exit=2 让整源跳过——2026-09-29 08:12 的抖音/小黑盒就是这样被双双跳过的。daemon 起来而
用户没开 Chrome 时同样全灭，故再补一层「拉起用户 Chrome」自愈。

不触真实 daemon/Chrome：`_run` 被打桩成按 argv 分派的假实现，`_spawn_daemon_foreground`
（autouse 夹具）与 `_launch_user_chrome` 一律打桩（测试里绝不允许真启动进程/Chrome）。
"""

from __future__ import annotations

import json
import time
from unittest.mock import Mock

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
    """按 argv 分派的假 `_run`：status 依次返回预设结果。"""

    def __init__(self, statuses, *, session_result=None) -> None:
        self.statuses = list(statuses)
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
        if argv[0] == "session" and len(argv) > 1 and argv[1] == "start":
            code, payload = self.session_result
            return BskResult(returncode=code, stdout=json.dumps(payload), stderr="")
        return BskResult(returncode=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def spawned_daemons(monkeypatch) -> list[tuple[str, ...]]:
    """daemon 拉起已改走 `_spawn_daemon_foreground`；测试里一律打桩，绝不真起子进程。"""
    spawned: list[tuple[str, ...]] = []

    def fake_spawn(command, log) -> None:
        spawned.append(tuple(command))
        log("cmd=daemon start --foreground 已派发（测试桩）")

    monkeypatch.setattr(bsk_browser, "_spawn_daemon_foreground", fake_spawn)
    return spawned


def _browser(fake: FakeRun, logs: list[str]) -> BskBrowser:
    browser = BskBrowser(command=("bsk",), logger=logs.append)
    browser._run = fake  # type: ignore[method-assign]
    return browser


def _kinds(calls: list[list[str]]) -> list[str]:
    return [c[0] for c in calls]


def test_daemon_down_is_started_then_session_runs(monkeypatch, spawned_daemons):
    """daemon 不在 → 自动拉起 → 会话照常建立（这条就是今早 08:12 缺失的自愈）。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    fake = FakeRun([(2, DAEMON_DOWN), (0, READY)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    assert browser.start() == "s-1"
    assert spawned_daemons == [("bsk",)]
    assert _kinds(fake.calls) == ["status", "status", "session"]
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


def test_no_browser_connected_triggers_launch(monkeypatch):
    """daemon 在但没连浏览器：不再只提示，而是尝试拉起用户 Chrome（扩展连上后自愈）。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bsk_browser, "_last_browser_launch_at", 0.0)
    launched: list[str] = []
    monkeypatch.setattr(
        bsk_browser, "_launch_user_chrome", lambda: launched.append("chrome") or "C:/chrome.exe"
    )
    fake = FakeRun([(0, NO_BROWSER), (0, READY)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    browser.start()

    assert launched == ["chrome"]
    assert any("尝试拉起用户 Chrome" in line for line in logs)
    assert any("Chrome 已连接" in line for line in logs)


def test_launch_happens_before_session_start(monkeypatch):
    """0 浏览器 → 拉起一次 → 浏览器连上 → start() 拿到 session_id；拉起须早于 session start。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bsk_browser, "_last_browser_launch_at", 0.0)
    order: list[str] = []

    def fake_launch() -> str:
        order.append("launch")
        return "C:/chrome.exe"

    monkeypatch.setattr(bsk_browser, "_launch_user_chrome", fake_launch)
    fake = FakeRun([(0, NO_BROWSER), (0, READY)])
    logs: list[str] = []
    browser = _browser(fake, logs)
    real_run = fake

    def recording_run(args, **kwargs):
        if list(args)[0] == "session":
            order.append("session")
        return real_run(args, **kwargs)

    browser._run = recording_run  # type: ignore[method-assign]

    assert browser.start() == "s-1"
    assert order == ["launch", "session"]


def test_launch_exception_does_not_block_session_start(monkeypatch):
    """拉起 Chrome 抛错也不能炸：记下原因后继续走 session start。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bsk_browser, "_last_browser_launch_at", 0.0)

    def boom() -> str:
        raise RuntimeError("找不到 Chrome")

    monkeypatch.setattr(bsk_browser, "_launch_user_chrome", boom)
    fake = FakeRun([(0, NO_BROWSER)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    assert browser.start() == "s-1"
    assert any("找不到 Chrome" in line for line in logs)
    assert fake.calls[-1][0:3] == ["session", "start", "--json"]


def test_launch_not_called_when_browser_connected(monkeypatch):
    """已连浏览器时绝不打扰正常路径：_launch_user_chrome 一次都不能被调用。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bsk_browser, "_last_browser_launch_at", 0.0)
    launch = Mock(side_effect=AssertionError("有浏览器连接时不应拉起 Chrome"))
    monkeypatch.setattr(bsk_browser, "_launch_user_chrome", launch)
    fake = FakeRun([(0, READY)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    assert browser.start() == "s-1"

    launch.assert_not_called()
    assert _kinds(fake.calls) == ["status", "session"]


def test_cooldown_suppresses_second_launch(monkeypatch):
    """冷却窗口内第二个源不再拉起 Chrome（19 个源不该各拉一次）。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bsk_browser, "_last_browser_launch_at", 0.0)
    launch = Mock(return_value="C:/chrome.exe")
    monkeypatch.setattr(bsk_browser, "_launch_user_chrome", launch)

    first = _browser(FakeRun([(0, NO_BROWSER), (0, READY)]), [])
    assert first.start() == "s-1"
    assert launch.call_count == 1

    second_logs: list[str] = []
    second = _browser(FakeRun([(0, NO_BROWSER)]), second_logs)
    assert second.start() == "s-1"

    assert launch.call_count == 1
    assert any("冷却窗口" in line for line in second_logs)


def test_session_failure_includes_no_browser_hint(monkeypatch):
    """session start 失败时把「没连浏览器」写进错误里，排障不用去翻 daemon 日志。"""
    monkeypatch.setattr(bsk_browser, "DAEMON_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bsk_browser, "BROWSER_CONNECT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(bsk_browser, "_last_browser_launch_at", 0.0)
    monkeypatch.setattr(bsk_browser, "_launch_user_chrome", lambda: "C:/chrome.exe")
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

    def boom(command, log):
        raise RuntimeError("bsk 不在 PATH")

    monkeypatch.setattr(bsk_browser, "_spawn_daemon_foreground", boom)
    fake = FakeRun([(2, DAEMON_DOWN)])
    logs: list[str] = []
    browser = _browser(fake, logs)

    assert browser.start() == "s-1"
    assert any("daemon start err" in line for line in logs)

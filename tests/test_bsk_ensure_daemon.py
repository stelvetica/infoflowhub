"""ensure_daemon 拉起方式回归检查：必须走 foreground 子进程。

`bsk daemon start`（脱离模式）在禁止 breakaway 的 Job Object 宿主里（登录计划任务拉起的
uvicorn）必被 bsk 拒绝（exit=2），foreground 子进程是唯一可行的程序化拉起方式。
全程打桩，不碰真实 daemon。
"""

from __future__ import annotations

import subprocess
from unittest import mock

from connectors._shared import bsk_browser
from connectors._shared.bsk_browser import BskBrowser


def test_spawn_daemon_foreground_uses_foreground_mode() -> None:
    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs

    with mock.patch.object(bsk_browser.subprocess, "Popen", FakePopen):
        bsk_browser._spawn_daemon_foreground(("bsk.exe",), lambda msg: None)

    assert captured["argv"] == ["bsk.exe", "daemon", "start", "--foreground"]
    # 输出必须静默、不阻塞宿主：foreground daemon 会一直运行到闲置自退。
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == subprocess.DEVNULL


def test_ensure_daemon_skips_spawn_when_status_ok() -> None:
    browser = BskBrowser()
    browser._daemon_status = lambda: ({"browsers": []}, "")  # type: ignore[assignment]
    with mock.patch.object(bsk_browser, "_spawn_daemon_foreground") as spawn:
        browser.ensure_daemon()
    spawn.assert_not_called()


def test_ensure_daemon_spawns_when_daemon_absent() -> None:
    browser = BskBrowser()
    statuses = iter([
        (None, "status exit=2"),  # 首次探活：不在
        ({"browsers": [{"instance_id": "x"}]}, ""),  # 拉起后：就绪
    ])
    browser._daemon_status = lambda: next(statuses)  # type: ignore[assignment]
    with mock.patch.object(bsk_browser, "_spawn_daemon_foreground") as spawn:
        browser.ensure_daemon()
    spawn.assert_called_once()

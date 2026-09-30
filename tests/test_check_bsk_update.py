"""check_bsk_update.ps1：扩展版本领先 CLI 时自动 `bsk update -y` 追平。

用桩 bsk（临时目录里的 bsk.cmd）驱动脚本，不碰真实安装、不联网。
断言对象是脚本写出的日志文件（stdout 受控制台代码页影响，日志是确定的 UTF-8）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_bsk_update.ps1"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="脚本与 bsk 都只在 Windows 上跑")

# 按参数分派的桩：update --check 吐 update-check.json，update -y 做「升级」（覆盖两个 json），
# status 吐 status.json。升级失败用 BSK_STUB_UPGRADE_EXIT 模拟。
_STUB = """@echo off
if "%1"=="update" goto update
if "%1"=="status" goto status
exit /b 0
:update
if "%2"=="--check" goto check
if "%2"=="-y" goto apply
exit /b 0
:apply
if "%BSK_STUB_UPGRADE_EXIT%"=="0" goto apply_ok
exit /b %BSK_STUB_UPGRADE_EXIT%
:apply_ok
copy /y "%~dp0update-check-after.json" "%~dp0update-check.json" >nul
copy /y "%~dp0status-after.json" "%~dp0status.json" >nul
exit /b 0
:check
type "%~dp0update-check.json"
exit /b 0
:status
type "%~dp0status.json"
exit /b 0
"""


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="ascii")


def _status(daemon: str, ext: str, *, skew: list | None = None) -> dict:
    return {
        "daemon_version": daemon,
        "version_skew_browsers": [] if skew is None else skew,
        "browsers": [{"browser_name": "chrome", "extension_version": ext}],
    }


def _copy_script(tmp_path: Path) -> None:
    """脚本副本放在 <tmp>/scripts/ 下，日志便会落到 <tmp>/runtime/（不污染仓库）。"""
    script_dir = tmp_path / "scripts"
    script_dir.mkdir(exist_ok=True)
    shutil.copyfile(SCRIPT, script_dir / "check_bsk_update.ps1")


def _make_stub(
    tmp_path: Path,
    *,
    cli: str,
    latest: str,
    ext: str,
    daemon: str = "0.3.1",
    after_cli: str = "0.3.2",
    after_daemon: str = "0.3.2",
) -> Path:
    """铺一个按参数分派的 bsk 桩，返回桩目录。"""
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "bsk.cmd").write_text(_STUB, encoding="ascii")
    _write(stub / "update-check.json", {"status": "up_to_date", "current_version": cli, "latest_version": latest})
    _write(stub / "update-check-after.json", {"status": "up_to_date", "current_version": after_cli, "latest_version": after_cli})
    _write(stub / "status.json", _status(daemon, ext))
    _write(stub / "status-after.json", _status(after_daemon, ext))
    return stub


def _prepare(tmp_path: Path, **kwargs) -> Path:
    _copy_script(tmp_path)
    return _make_stub(tmp_path, **kwargs)


def _run(
    tmp_path: Path,
    stub: Path | None,
    *,
    upgrade_exit: int = 0,
    userprofile: Path | None = None,
    path_override: str | None = None,
) -> tuple[int, str]:
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("找不到 powershell")
    env = dict(os.environ)
    env["BSK_STUB_UPGRADE_EXIT"] = str(upgrade_exit)
    if path_override is not None:
        env["PATH"] = path_override
    elif stub is not None:
        env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
    if userprofile is not None:
        # Resolve-Bsk 的兜底会去 %USERPROFILE%\.local\bin 找真实 bsk，测「找不到」时须改掉它
        env["USERPROFILE"] = str(userprofile)
    script = tmp_path / "scripts" / "check_bsk_update.ps1"
    proc = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    log = tmp_path / "runtime" / "bsk_update_check.log"
    return proc.returncode, (log.read_text(encoding="utf-8") if log.is_file() else "")


def test_extension_ahead_upgrades_cli_and_reports_after(tmp_path: Path):
    """扩展 0.3.2 领先 CLI 0.3.1：执行追平，且报告用的是升级后的版本。"""
    stub = _prepare(tmp_path, cli="0.3.1", latest="0.3.2", ext="0.3.2")
    code, log = _run(tmp_path, stub)
    assert "执行 bsk update -y 追平" in log
    assert "CLI 0.3.2 -> 0.3.2 (up_to_date)" in log  # 升级后复检的结果
    assert "daemon 0.3.2" in log
    assert code == 0


def test_extension_matching_cli_takes_no_action(tmp_path: Path):
    """扩展与 CLI 同版本：不升级、不提示。"""
    stub = _prepare(tmp_path, cli="0.3.2", latest="0.3.2", ext="0.3.2")
    code, log = _run(tmp_path, stub)
    assert "追平" not in log
    assert "仍领先" not in log
    assert code == 0


def test_extension_behind_cli_takes_no_action(tmp_path: Path):
    """扩展落后于 CLI：那是扩展自己渠道的事，脚本只报告不动作。"""
    stub = _prepare(tmp_path, cli="0.3.2", latest="0.3.2", ext="0.3.1")
    code, log = _run(tmp_path, stub)
    assert "追平" not in log
    assert "无版本错配" in log
    assert code == 0


def test_failed_upgrade_is_reported_and_flagged(tmp_path: Path):
    """升级失败：如实记退出码，并因扩展仍领先而以 2 退出（待处理项）。"""
    stub = _prepare(tmp_path, cli="0.3.1", latest="0.3.2", ext="0.3.2")
    code, log = _run(tmp_path, stub, upgrade_exit=1)
    assert "FAIL bsk update 退出码 1" in log
    assert "仍领先 CLI" in log
    assert code == 2


def test_missing_cli_fails_fast(tmp_path: Path):
    """bsk 不在 PATH 且用户目录下也没有：退出码 1，且不尝试升级。"""
    _copy_script(tmp_path)
    # PATH 只留系统目录：本机 ~/.local/bin 里装着真 bsk，不清掉就测不到「找不到」
    system_path = os.pathsep.join([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")])
    code, log = _run(tmp_path, None, userprofile=tmp_path / "nouser", path_override=system_path)
    assert "FAIL 未找到 bsk CLI" in log
    assert code == 1

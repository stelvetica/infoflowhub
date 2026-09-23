"""skill_runner：CLI 发现、超时杀进程树、非零退出码、JSON 容错。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from web.services import skill_runner


class _FakePopen:
    """替代 subprocess.Popen：按脚本返回/抛超时，并记录 communicate 调用。"""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "", timeout_once: bool = False):
        self.pid = 4321
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._timeout_once = timeout_once

    def communicate(self, timeout: int | None = None):
        if self._timeout_once:
            self._timeout_once = False
            raise subprocess.TimeoutExpired(cmd="node", timeout=timeout)
        return self._stdout, self._stderr


@pytest.fixture()
def cli_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 node 与 CLI 都「存在」，以便进入子进程分支。"""
    monkeypatch.setattr(skill_runner.shutil, "which", lambda name: "node")
    monkeypatch.setattr(skill_runner, "find_zcode_cli", lambda: Path("C:/fake/zcode.cjs"))


def test_find_zcode_cli_prefers_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cli = tmp_path / "zcode.cjs"
    cli.write_text("//", encoding="utf-8")
    monkeypatch.setenv("INFOFLOW_ZCODE_CLI", str(cli))
    assert skill_runner.find_zcode_cli() == cli


def test_find_zcode_cli_missing_env_override_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INFOFLOW_ZCODE_CLI", "C:/nope/zcode.cjs")
    assert skill_runner.find_zcode_cli() is None


def test_find_zcode_cli_uses_localappdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INFOFLOW_ZCODE_CLI", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    target = tmp_path / skill_runner.CLI_RELATIVE_PATH
    target.parent.mkdir(parents=True)
    target.write_text("//", encoding="utf-8")
    assert skill_runner.find_zcode_cli() == target


def test_find_zcode_cli_without_localappdata_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INFOFLOW_ZCODE_CLI", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert skill_runner.find_zcode_cli() is None


def test_run_skill_unavailable_when_cli_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(skill_runner.shutil, "which", lambda name: "node")
    monkeypatch.setattr(skill_runner, "find_zcode_cli", lambda: None)
    result = skill_runner.run_skill("portfolio-news", cwd=tmp_path)
    assert result["status"] == "unavailable"
    assert result["error"]
    assert result["finished_at"]


def test_run_skill_unavailable_when_node_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(skill_runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(skill_runner, "find_zcode_cli", lambda: Path("C:/fake/zcode.cjs"))
    assert skill_runner.run_skill("portfolio-news", cwd=tmp_path)["status"] == "unavailable"


def test_run_skill_timeout_kills_process_tree(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = _FakePopen(timeout_once=True)
    monkeypatch.setattr(skill_runner.subprocess, "Popen", lambda *a, **k: fake)
    killed: list[int] = []
    monkeypatch.setattr(skill_runner, "_kill_process_tree", lambda process: killed.append(process.pid))
    result = skill_runner.run_skill("portfolio-news", cwd=tmp_path, timeout=5)
    assert result["status"] == "timeout"
    assert killed == [fake.pid]
    assert "5" in result["error"]


def test_kill_process_tree_invokes_taskkill_on_windows(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(skill_runner.os, "name", "nt")
    monkeypatch.setattr(skill_runner.subprocess, "run", lambda command, **kwargs: calls.append(command))
    skill_runner._kill_process_tree(_FakePopen())
    assert calls and calls[0][:4] == ["taskkill", "/F", "/T", "/PID"]


def test_run_skill_error_uses_stderr_tail(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = _FakePopen(returncode=1, stderr="x" * 800 + "尾部错误")
    monkeypatch.setattr(skill_runner.subprocess, "Popen", lambda *a, **k: fake)
    result = skill_runner.run_skill("portfolio-news", cwd=tmp_path)
    assert result["status"] == "error"
    assert len(result["error"]) <= skill_runner.ERROR_TAIL_CHARS
    assert result["error"].endswith("尾部错误")


def test_run_skill_error_without_stderr_mentions_exit_code(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = _FakePopen(returncode=2, stderr="")
    monkeypatch.setattr(skill_runner.subprocess, "Popen", lambda *a, **k: fake)
    assert "2" in skill_runner.run_skill("portfolio-news", cwd=tmp_path)["error"]


def test_run_skill_success_tolerates_noisy_stdout(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = _FakePopen(returncode=0, stdout='日志噪声\n{"sessionId": "s1", "response": "ok"}\n收尾噪声')
    monkeypatch.setattr(skill_runner.subprocess, "Popen", lambda *a, **k: fake)
    result = skill_runner.run_skill("portfolio-news", cwd=tmp_path)
    assert result["status"] == "success"
    assert result["error"] == ""


def test_run_skill_success_even_when_stdout_unparsable(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = _FakePopen(returncode=0, stdout="完全不是 JSON")
    monkeypatch.setattr(skill_runner.subprocess, "Popen", lambda *a, **k: fake)
    assert skill_runner.run_skill("portfolio-news", cwd=tmp_path)["status"] == "success"


def test_run_skill_builds_expected_command(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured: dict[str, list[str]] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return _FakePopen(returncode=0)

    monkeypatch.setattr(skill_runner.subprocess, "Popen", fake_popen)
    skill_runner.run_skill("portfolio-announcements", cwd=tmp_path)
    command = captured["command"]
    assert command[:3] == ["node", str(Path("C:/fake/zcode.cjs")), "-p"]
    assert command[3] == "/skill portfolio-announcements 按 SKILL.md 完整执行本流程"
    assert command[4:] == ["--mode", "yolo", "--cwd", str(tmp_path), "--json"]


def test_run_skill_returns_error_when_popen_fails(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Popen 本身失败（node/CLI 被删、句柄耗尽）也必须返回 error，不能抛异常冲出调度循环。"""

    def boom(*_args, **_kwargs):
        raise OSError("句柄耗尽")

    monkeypatch.setattr(skill_runner.subprocess, "Popen", boom)
    result = skill_runner.run_skill("portfolio-news", cwd=tmp_path)
    assert result["status"] == "error"
    assert "句柄耗尽" in result["error"]
    assert result["finished_at"]
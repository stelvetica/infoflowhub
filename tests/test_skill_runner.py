"""skill_runner：CLI 发现、超时杀进程树、非零退出码、JSON 容错。"""

from __future__ import annotations

import os
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


def _write_provider_config(root: Path, version: str, endpoint: str) -> Path:
    """按 App 运行时缓存布局造一份 built-in provider 配置。"""
    path = root / "windows-x86_64" / version / f"endpoint-{endpoint}" / "zcode-builtin.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture()
def runtime_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """造一个受控的 App 运行时缓存根，并把 personal 配置指到临时文件（两条路径都确定）。"""
    monkeypatch.delenv(skill_runner.BUILTIN_PROVIDER_CONFIG_ENV, raising=False)
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(skill_runner, "PROVIDER_RUNTIME_ROOT", runtime_root)
    personal = tmp_path / "provider_config.json"
    personal.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(skill_runner, "PERSONAL_PROVIDER_CONFIG", personal)
    return runtime_root


def test_provider_config_env_prefers_newest_runtime_copy(runtime_cache: Path):
    """通配定位最新一份：路径含 App 版本与 endpoint 哈希，不能硬编码。"""
    old = _write_provider_config(runtime_cache, "3.14.3", "aaa")
    new = _write_provider_config(runtime_cache, "3.14.4", "bbb")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    env = skill_runner._provider_config_env()
    assert env[skill_runner.BUILTIN_PROVIDER_CONFIG_ENV] == str(new)
    assert env[skill_runner.PERSONAL_PROVIDER_CONFIG_ENV] == str(skill_runner.PERSONAL_PROVIDER_CONFIG)


def test_provider_config_env_skips_missing_personal(runtime_cache: Path):
    """personal 配置不存在时只补 builtin，不塞一个指向空文件的环境变量。"""
    _write_provider_config(runtime_cache, "3.14.4", "bbb")
    skill_runner.PERSONAL_PROVIDER_CONFIG.unlink()
    assert list(skill_runner._provider_config_env()) == [skill_runner.BUILTIN_PROVIDER_CONFIG_ENV]


def test_provider_config_env_keeps_app_injected_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """App 拉起的进程已带该变量：不覆盖，避免把 App 选定的那份换掉。"""
    monkeypatch.setenv(skill_runner.BUILTIN_PROVIDER_CONFIG_ENV, "C:/app/injected.json")
    monkeypatch.setattr(skill_runner, "PROVIDER_RUNTIME_ROOT", tmp_path)
    _write_provider_config(tmp_path, "3.14.4", "bbb")
    assert skill_runner._provider_config_env() == {}


def test_provider_config_env_empty_without_runtime_cache(runtime_cache: Path):
    """缓存不存在（如未装 App）：不补变量，让 CLI 按自己的逻辑报错，不掩盖问题。"""
    assert skill_runner._provider_config_env() == {}


def test_run_skill_injects_provider_config_env(cli_ready: None, runtime_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """注入必须落到子进程 env（本次回归的根因：定时流程缺这个变量 → skill 秒退）。"""
    config = _write_provider_config(runtime_cache, "3.14.4", "bbb")

    captured: dict[str, dict] = {}

    def fake_popen(command, **kwargs):
        captured["kwargs"] = kwargs
        return _FakePopen(returncode=0)

    monkeypatch.setattr(skill_runner.subprocess, "Popen", fake_popen)
    skill_runner.run_skill("portfolio-news", cwd=tmp_path)
    assert captured["kwargs"]["env"][skill_runner.BUILTIN_PROVIDER_CONFIG_ENV] == str(config)

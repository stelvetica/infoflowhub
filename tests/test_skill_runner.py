"""skill_runner：CLI 发现、超时杀进程树、非零退出码、JSON 容错。"""

from __future__ import annotations

import json
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


@pytest.fixture(autouse=True)
def _isolated_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """单测不读真实 provider 配置：候选固定为 p1/m1 → p1/m1b → p2/m2，钉死副本也只写 tmp。"""
    personal = tmp_path / "autouse_provider_config.json"
    personal.write_text(
        json.dumps(
            {
                "config": {
                    "providerOrder": ["p1", "p2"],
                    "providerConfigRules": {
                        "providerRules": [
                            {"providerId": "p1", "enabled": True, "config": {"modelOrder": ["m1", "m1b"]}},
                            {"providerId": "p2", "enabled": True, "config": {"modelOrder": ["m2"]}},
                        ]
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_runner, "PERSONAL_PROVIDER_CONFIG", personal)
    monkeypatch.setattr(skill_runner, "PINNED_PROVIDER_CONFIG", tmp_path / "pinned.json")
    # 明确钉死候选链：既不读真实配置，也不依赖 DEFAULT_SKILL_MODELS 的内容
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "p1/m1,p1/m1b,p2/m2")


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
    # 剩余预算按秒取整，实际传给子进程的可能比 5 少 1 秒，只断言语义不锁死数字
    assert "未完成，已终止进程树" in result["error"]


def test_kill_process_tree_invokes_taskkill_on_windows(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(skill_runner.os, "name", "nt")
    monkeypatch.setattr(skill_runner.subprocess, "run", lambda command, **kwargs: calls.append(command))
    skill_runner._kill_process_tree(_FakePopen())
    assert calls and calls[0][:4] == ["taskkill", "/F", "/T", "/PID"]


def test_run_skill_error_uses_stderr_tail(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(skill_runner, "SKILL_MODEL_ATTEMPTS", 1)
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


def test_run_skill_fails_over_to_next_model(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """首次模型硬失败，换下一个候选模型后成功：返回 success，且钉死副本已切到备选。"""
    attempts: list[int] = []

    def fake_popen(command, **kwargs):
        attempts.append(1)
        return _FakePopen(
            returncode=1 if len(attempts) == 1 else 0,
            stderr="R4 Coder: The request could not be processed.\nError: Turn execution failed",
        )

    monkeypatch.setattr(skill_runner.subprocess, "Popen", fake_popen)
    result = skill_runner.run_skill("portfolio-news", cwd=tmp_path)
    assert result["status"] == "success"
    assert len(attempts) == 2
    assert result["error"] == ""
    pinned = json.loads(skill_runner.PINNED_PROVIDER_CONFIG.read_text(encoding="utf-8"))
    assert pinned["config"]["defaultModelSelection"] == {"providerId": "p1", "modelId": "m1b"}


def test_run_skill_exhausts_model_candidates(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """候选全部失败：返回 error，并在文案里点名试过的模型，便于面板定位。"""
    attempts: list[int] = []

    def fake_popen(command, **kwargs):
        attempts.append(1)
        return _FakePopen(returncode=1, stderr="The stream was terminated by the server.")

    monkeypatch.setattr(skill_runner.subprocess, "Popen", fake_popen)
    result = skill_runner.run_skill("portfolio-news", cwd=tmp_path)
    assert result["status"] == "error"
    assert len(attempts) == 3
    assert "p1/m1" in result["error"] and "p2/m2" in result["error"] and "均失败" in result["error"]


def test_run_skill_timeout_is_not_retried_across_models(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """超时不换模型：单次已耗满整个 skill 的时间预算，再试只会拖长整条流程。"""
    attempts: list[int] = []

    def fake_popen(command, **kwargs):
        attempts.append(1)
        return _FakePopen(timeout_once=True)

    monkeypatch.setattr(skill_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(skill_runner, "_kill_process_tree", lambda process: None)
    assert skill_runner.run_skill("portfolio-news", cwd=tmp_path, timeout=5)["status"] == "timeout"
    assert len(attempts) == 1


def test_run_skill_unavailable_is_not_retried(cli_ready: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """CLI 缺失不换模型：换模型解决不了环境问题。"""
    attempts: list[int] = []

    def fake_popen(command, **kwargs):
        attempts.append(1)
        return _FakePopen(returncode=0)

    monkeypatch.setattr(skill_runner, "find_zcode_cli", lambda: None)
    monkeypatch.setattr(skill_runner.subprocess, "Popen", fake_popen)
    assert skill_runner.run_skill("portfolio-news", cwd=tmp_path)["status"] == "unavailable"
    assert attempts == []


def _write_provider_config(root: Path, version: str, endpoint: str) -> Path:
    """按 App 运行时缓存布局造一份 built-in provider 配置。"""
    path = root / "windows-x86_64" / version / f"endpoint-{endpoint}" / "zcode-builtin.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture()
def runtime_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """造一个受控的 App 运行时缓存根，personal/pinned 配置都指到临时文件（三条路径都确定）。"""
    monkeypatch.delenv(skill_runner.BUILTIN_PROVIDER_CONFIG_ENV, raising=False)
    # 关掉模型钉死：本 fixture 只验 builtin 定位，personal 因此回退原件（钉死路径另有用例）
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "")
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(skill_runner, "PROVIDER_RUNTIME_ROOT", runtime_root)
    personal = tmp_path / "provider_config.json"
    personal.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(skill_runner, "PERSONAL_PROVIDER_CONFIG", personal)
    monkeypatch.setattr(skill_runner, "PINNED_PROVIDER_CONFIG", tmp_path / "skill_provider_config.json")
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


def test_provider_config_env_empty_without_runtime_cache(runtime_cache: Path):
    """缓存不存在（如未装 App）：不补变量，让 CLI 按自己的逻辑报错，不掩盖问题。"""
    assert skill_runner._provider_config_env() == {}


def _pin_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """把 personal/pinned 两份配置指到 tmp_path（绝不碰真实用户配置里的密钥），并清掉模型环境变量。"""
    personal = tmp_path / "provider_config.json"
    pinned = tmp_path / "runtime" / "skill_provider_config.json"
    monkeypatch.setattr(skill_runner, "PERSONAL_PROVIDER_CONFIG", personal)
    monkeypatch.setattr(skill_runner, "PINNED_PROVIDER_CONFIG", pinned)
    monkeypatch.delenv(skill_runner.SKILL_MODEL_ENV, raising=False)
    return personal, pinned


def _write_source_config(personal: Path) -> dict:
    """造一份源 personal 配置：defaultModelSelection 是别的模型，另带须原样保留的键与假密钥。"""
    data = {
        "config": {"defaultModelSelection": {"providerId": "custom:other", "modelId": "other-1"}, "note": "保留"},
        "secret": "sk-fake",
    }
    personal.parent.mkdir(parents=True, exist_ok=True)
    personal.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def test_pinned_personal_config_repins_default_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """源配置指向别的模型时，副本被掰回默认链首模型，且其余键与源逐一致。"""
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    source = _write_source_config(personal)
    assert skill_runner.pinned_personal_config() == pinned
    written = json.loads(pinned.read_text(encoding="utf-8"))
    assert written["config"]["defaultModelSelection"] == {
        "providerId": "magpie",
        "modelId": "r4coder/deepseek-v4.1-flash",
    }
    assert written["config"]["note"] == source["config"]["note"]
    assert written["secret"] == source["secret"]


def test_pinned_personal_config_honors_env_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """环境变量覆盖默认值，并按第一个 "/" 切分 providerId/modelId。"""
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    _write_source_config(personal)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "custom:agnes/agnes-3.0-flash")
    skill_runner.pinned_personal_config()
    assert json.loads(pinned.read_text(encoding="utf-8"))["config"]["defaultModelSelection"] == {
        "providerId": "custom:agnes",
        "modelId": "agnes-3.0-flash",
    }


def test_pinned_personal_config_empty_env_disables_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """置空串 = 关闭钉死：不生成副本，personal 回退原件路径（降级开关）。"""
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    _write_source_config(personal)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "")
    monkeypatch.delenv(skill_runner.BUILTIN_PROVIDER_CONFIG_ENV, raising=False)
    runtime_root = tmp_path / "runtime_cache"
    monkeypatch.setattr(skill_runner, "PROVIDER_RUNTIME_ROOT", runtime_root)
    _write_provider_config(runtime_root, "3.14.4", "bbb")
    assert skill_runner.pinned_personal_config() is None
    assert not pinned.exists()
    assert skill_runner._provider_config_env()[skill_runner.PERSONAL_PROVIDER_CONFIG_ENV] == str(personal)


@pytest.mark.parametrize("bad_source", ["missing", "corrupt"])
def test_pinned_personal_config_returns_none_on_unusable_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_source: str
):
    """源配置缺失/JSON 损坏：返回 None 且不抛异常（不得阻断 skill 执行）。"""
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    if bad_source == "corrupt":
        personal.write_text("{ 不是 JSON", encoding="utf-8")
    assert skill_runner.pinned_personal_config() is None
    assert not pinned.exists()


def test_pinned_personal_config_returns_none_on_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """写入失败（如磁盘满）：返回 None 且不抛异常。"""
    personal, _pinned = _pin_paths(tmp_path, monkeypatch)
    _write_source_config(personal)

    def boom(*_args, **_kwargs):
        raise OSError("磁盘满")

    monkeypatch.setattr(skill_runner.os, "replace", boom)
    assert skill_runner.pinned_personal_config() is None


def test_provider_config_env_app_injected_keeps_builtin_but_adds_pinned_personal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """App 已注入 builtin 时仍补钉死后的 personal。

    本条断言本次按新行为改写：旧实现此处直接 `return {}`，会让「App 拉起服务」这条路径漏掉
    模型钉死——skill 的模型随「谁拉起服务」而变，故必须补 personal。
    """
    monkeypatch.setenv(skill_runner.BUILTIN_PROVIDER_CONFIG_ENV, "C:/app/injected.json")
    monkeypatch.setattr(skill_runner, "PROVIDER_RUNTIME_ROOT", tmp_path / "runtime_cache")
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    _write_source_config(personal)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, skill_runner.DEFAULT_SKILL_MODELS)
    env = skill_runner._provider_config_env()
    assert skill_runner.BUILTIN_PROVIDER_CONFIG_ENV not in env  # 不覆盖 App 注入值
    assert env[skill_runner.PERSONAL_PROVIDER_CONFIG_ENV] == str(pinned)
    assert json.loads(pinned.read_text(encoding="utf-8"))["config"]["defaultModelSelection"]["modelId"] == "r4coder/deepseek-v4.1-flash"


def test_provider_config_env_app_injected_without_pin_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """钉死不可用（源配置缺失）时，App 注入分支保持改动前行为：返回 {}。"""
    monkeypatch.setenv(skill_runner.BUILTIN_PROVIDER_CONFIG_ENV, "C:/app/injected.json")
    monkeypatch.setattr(skill_runner, "PROVIDER_RUNTIME_ROOT", tmp_path / "runtime_cache")
    personal, _pinned = _pin_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, skill_runner.DEFAULT_SKILL_MODELS)
    assert not personal.exists()
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


def _write_provider_aware_config(
    personal: Path, rules: list[dict], order: list[str], model_rules: list[dict] | None = None
) -> None:
    """造一份带 providerRules 的源配置：用于验证钉死目标失效时的回退。"""
    data = {
        "config": {
            "providerOrder": order,
            "providerConfigRules": {"providerRules": rules},
            "modelConfigRules": {"providerModelRules": model_rules or []},
            "defaultModelSelection": {"providerId": "custom:r4", "modelId": "glm-5.3-flash"},
            "note": "保留",
        },
        "secret": "sk-fake",
    }
    personal.parent.mkdir(parents=True, exist_ok=True)
    personal.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _model_rule(provider_id: str, model_id: str, levels: list[str]) -> dict:
    """造一条带推理档位规格的模型规则（CLI 要求选中时必须显式给档）。"""
    return {
        "providerId": provider_id,
        "modelId": model_id,
        "config": {"optionSpecs": {"reasoningLevel": {"values": levels}}},
    }


def test_pinned_personal_config_falls_back_to_same_model_on_enabled_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """钉死的 provider 被禁用：跨 provider 找同名模型，而不是 CLI 默认的首个模型。

    2026-10-29 晨间流程的实际故障：custom:r4 被禁用后 CLI 静默回退到 magpie 的首个模型
    （慢一个数量级的推理模型），portfolio-news 因此超时。
    """
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "custom:r4/glm-5.3-flash")
    _write_provider_aware_config(
        personal,
        rules=[
            {"providerId": "magpie", "enabled": True, "config": {"modelOrder": ["group/slow-reasoner", "r4coder/glm-5.3-flash"]}},
            {"providerId": "custom:r4", "enabled": False, "config": {"modelOrder": ["glm-5.3-flash"]}},
        ],
        order=["magpie", "custom:r4"],
        model_rules=[_model_rule("magpie", "r4coder/glm-5.3-flash", ["low", "high", "max"])],
    )
    assert skill_runner.pinned_personal_config() == pinned
    written = json.loads(pinned.read_text(encoding="utf-8"))
    assert written["config"]["defaultModelSelection"] == {
        "providerId": "magpie",
        "modelId": "r4coder/glm-5.3-flash",
        "options": {"reasoningLevel": "low"},
    }
    assert written["config"]["note"] == "保留"
    assert written["secret"] == "sk-fake"


def test_pinned_personal_config_falls_back_to_first_enabled_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """没有同名模型时，退到 providerOrder 里首个可用 provider 的首个模型。"""
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "custom:r4/glm-5.3-flash")
    _write_provider_aware_config(
        personal,
        rules=[
            {"providerId": "magpie", "enabled": True, "config": {"modelOrder": ["group/slow-reasoner"]}},
            {"providerId": "custom:r4", "enabled": False, "config": {"modelOrder": ["glm-5.3-flash"]}},
        ],
        order=["magpie", "custom:r4"],
        model_rules=[_model_rule("magpie", "group/slow-reasoner", ["low", "high", "xhigh"])],
    )
    skill_runner.pinned_personal_config()
    assert json.loads(pinned.read_text(encoding="utf-8"))["config"]["defaultModelSelection"] == {
        "providerId": "magpie",
        "modelId": "group/slow-reasoner",
        "options": {"reasoningLevel": "low"},
    }


def test_pinned_personal_config_replaces_removed_model_on_same_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """provider 还在、钉死的模型已被移除：同 provider 内换首个可用模型，不让 CLI 静默漂走。"""
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "custom:r4/glm-5.3-flash")
    _write_provider_aware_config(
        personal,
        rules=[{"providerId": "custom:r4", "enabled": True, "config": {"modelOrder": ["deepseek-v4.1-flash"]}}],
        order=["custom:r4"],
    )
    skill_runner.pinned_personal_config()
    assert json.loads(pinned.read_text(encoding="utf-8"))["config"]["defaultModelSelection"] == {
        "providerId": "custom:r4",
        "modelId": "deepseek-v4.1-flash",
    }


def test_pinned_personal_config_keeps_enabled_provider_without_reasoning_levels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """钉死的 provider 仍可用、模型也没有推理档位时原样保留，不额外加 options。"""
    personal, pinned = _pin_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "custom:r4/glm-5.3-flash")
    _write_provider_aware_config(
        personal,
        rules=[
            {"providerId": "magpie", "enabled": True, "config": {"modelOrder": ["group/slow-reasoner"]}},
            {"providerId": "custom:r4", "enabled": True, "config": {"modelOrder": ["glm-5.3-flash"]}},
        ],
        order=["magpie", "custom:r4"],
    )
    skill_runner.pinned_personal_config()
    assert json.loads(pinned.read_text(encoding="utf-8"))["config"]["defaultModelSelection"] == {
        "providerId": "custom:r4",
        "modelId": "glm-5.3-flash",
    }


def test_model_candidates_uses_declared_chain_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """候选链严格按声明的顺序解析：不自动补同厂商/其他 provider 的模型。"""
    personal, _pinned = _pin_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(
        skill_runner.SKILL_MODEL_ENV,
        "magpie/r4coder/deepseek-v4.1-flash,magpie/group/freedsf4.1",
    )
    _write_provider_aware_config(
        personal,
        rules=[
            {
                "providerId": "magpie",
                "enabled": True,
                "config": {
                    "modelOrder": [
                        "group/freedsf4.1",
                        "r4coder/deepseek-v4.1-flash",
                        "r4coder/glm-5.3-flash",
                        "coderapi-oai/gpt-6.1-sol",
                    ]
                },
            },
            {"providerId": "custom:r4", "enabled": False, "config": {"modelOrder": ["glm-5.3-flash"]}},
        ],
        order=["magpie", "custom:r4"],
        model_rules=[
            _model_rule("magpie", "r4coder/glm-5.3-flash", ["low", "high", "max"]),
            _model_rule("magpie", "r4coder/deepseek-v4.1-flash", ["low", "high"]),
            _model_rule("magpie", "group/freedsf4.1", ["low", "high", "xhigh"]),
        ],
    )
    candidates = skill_runner._model_candidates()
    assert [(item["providerId"], item["modelId"]) for item in candidates] == [
        ("magpie", "r4coder/deepseek-v4.1-flash"),
        ("magpie", "group/freedsf4.1"),
    ]
    assert candidates[0]["options"] == {"reasoningLevel": "low"}
    assert candidates[1]["options"] == {"reasoningLevel": "low"}


def test_model_candidates_default_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """默认链：deepseek-v4.1-flash 主用，group/freedsf4.1 备选（2026-10-30 故障后定）。"""
    personal, _pinned = _pin_paths(tmp_path, monkeypatch)
    _write_provider_aware_config(
        personal,
        rules=[
            {
                "providerId": "magpie",
                "enabled": True,
                "config": {
                    "modelOrder": [
                        "group/freedsf4.1",
                        "r4coder/deepseek-v4.1-flash",
                        "r4coder/glm-5.3-flash",
                    ]
                },
            }
        ],
        order=["magpie"],
    )
    candidates = skill_runner._model_candidates()
    assert [(item["providerId"], item["modelId"]) for item in candidates] == [
        ("magpie", "r4coder/deepseek-v4.1-flash"),
        ("magpie", "group/freedsf4.1"),
    ]


def test_model_candidates_dedupes_resolved_selections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """链里两项被回退解析成同一个模型时只保留一个（ENV 里重复写也不放大尝试次数）。"""
    personal, _pinned = _pin_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "custom:r4/glm-5.3-flash,custom:r4/glm-5.3-flash")
    _write_provider_aware_config(personal, rules=[], order=[], model_rules=[])
    assert skill_runner._model_candidates() == [
        {"providerId": "custom:r4", "modelId": "glm-5.3-flash"}
    ]


def test_model_candidates_empty_when_pin_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """钉死关闭（INFOFLOW_SKILL_MODEL 置空）时不生成候选，调用方沿用 App 默认模型。"""
    personal, _pinned = _pin_paths(tmp_path, monkeypatch)
    _write_source_config(personal)
    monkeypatch.setenv(skill_runner.SKILL_MODEL_ENV, "")
    assert skill_runner._model_candidates() == []

"""无头执行 ZCode skill 的薄封装：发现 CLI、跑子进程、超时控制、失败退避重试。

职责单一：只负责「跑一次 skill 并回传结果」，不碰状态文件、不碰视图。
ponytail: 天花板——只判退出码，不解析 token 用量、不看产物是否其实已生成；升级路径是接入 CLI 返回的 usage 做成本统计。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from infra.utf8_json import dump_json_utf8, load_json_utf8

BASE_DIR = Path(__file__).resolve().parents[2]

# 严格按此顺序执行：新闻影响分析先跑，公告清单后跑（公告产物不能因前者失败而缺失）
SKILLS = ("portfolio-news", "portfolio-announcements")
SKILL_TIMEOUT_SECONDS = 1200  # 每个 skill 20 分钟
# 失败后按此间隔（秒）退避重试，最后一个间隔用完即收工。
# 服务端偶发掐流（`The stream was terminated by the server`）实测十几分钟到半小时自愈——
# 2026-10-09/10-10 连续两天晨间流程撞上，失败即放弃只能等次日调度或人工补跑；
# 第二次等待拉长是为跨过更长的故障窗口。只重试非零退出码，timeout/unavailable 见 run_skill。
SKILL_RETRY_DELAYS_SECONDS = (180, 600)
SKILL_PROMPT_TEMPLATE = "/skill {name} 按 SKILL.md 完整执行本流程"
# 相对 %LOCALAPPDATA% 的 CLI 位置；不硬编码用户名
CLI_RELATIVE_PATH = Path("Programs") / "ZCode" / "resources" / "glm" / "zcode.cjs"
ERROR_TAIL_CHARS = 500
# CLI 定位 built-in provider 配置的环境变量：桌面 App 会给它拉起的进程注入，本服务由计划任务
# 独立拉起、拿不到，CLI 便退化为按 zcode.cjs 位置猜路径而必然失败（详见 _provider_config_env）。
BUILTIN_PROVIDER_CONFIG_ENV = "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"
PERSONAL_PROVIDER_CONFIG_ENV = "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"
# App 运行时缓存根：<根>/<平台>/<App 版本>/endpoint-<哈希>/zcode-builtin.json
PROVIDER_RUNTIME_ROOT = Path.home() / ".zcode" / "v2" / "runtime" / "provider"
# personal 配置与 App 注入值同路径（CLI 的默认回落值也是它）
PERSONAL_PROVIDER_CONFIG = Path.home() / ".zcode" / "v2" / "provider_config.json"
# CLI v0.16.9 的 headless 模式没有 --model 参数（传了只打帮助），执行模型只能由 personal
# provider 配置里的 config.defaultModelSelection 决定；桌面 App 可能被用户切成别的模型，
# 故本服务派生一份只改该字段的副本喂给子进程，把 skill 执行模型钉死。
SKILL_MODEL_ENV = "INFOFLOW_SKILL_MODEL"  # "providerId/modelId"；置空串 = 关闭钉死，回退现状
DEFAULT_SKILL_MODEL = "custom:r4/glm-5.3-flash"
# 副本落在 runtime/（已被 .gitignore 忽略）：它含 personal 配置里的密钥，绝不能入库进日志
PINNED_PROVIDER_CONFIG = BASE_DIR / "runtime" / "skill_provider_config.json"


def find_zcode_cli() -> Path | None:
    """定位 zcode.cjs：优先环境变量覆盖，否则用 LOCALAPPDATA 拼默认路径；不存在返回 None。"""
    override = os.environ.get("INFOFLOW_ZCODE_CLI", "").strip()
    if override:
        path = Path(override)
        return path if path.is_file() else None
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_appdata:
        return None
    path = Path(local_appdata) / CLI_RELATIVE_PATH
    return path if path.is_file() else None


def _parse_skill_model() -> tuple[str, str] | None:
    """把 SKILL_MODEL_ENV 解析成 (providerId, modelId)；未设用默认，置空/格式非法返回 None。"""
    raw = os.environ.get(SKILL_MODEL_ENV)
    value = (DEFAULT_SKILL_MODEL if raw is None else raw).strip()
    if not value:
        return None  # 显式置空 = 关闭钉死
    # 按最后一个 "/" 切分：providerId 自身含 ":" 但不含 "/"（如 "custom:r4/glm-5.3-flash"）。
    provider_id, sep, model_id = value.rpartition("/")
    if not sep or not provider_id or not model_id:
        return None  # 解析失败 → 不钉死
    return provider_id, model_id


def pinned_personal_config() -> Path | None:
    """把 personal provider 配置的 defaultModelSelection 改成钉死模型，写一份副本并回其路径。

    模型串约定 `providerId/modelId`，按**最后一个** "/" 切分（providerId 自身含 ":" 但不含 "/"，
    如 "custom:r4/glm-5.3-flash" → providerId="custom:r4"、modelId="glm-5.3-flash"）。

    未设 SKILL_MODEL_ENV 时用 DEFAULT_SKILL_MODEL；置空串或解析失败则不钉死（返回 None）。
    副本经 infra.utf8_json 原子写入 PINNED_PROVIDER_CONFIG（先临时文件再 os.replace）。
    任何异常（源读不到、JSON 坏、写失败）一律返回 None，绝不抛出、绝不阻断 skill 执行。
    副本含 personal 配置里的密钥，只落在 runtime/ 下，禁止入库或进日志。
    """
    parsed = _parse_skill_model()
    if parsed is None:
        return None
    provider_id, model_id = parsed
    try:
        config = load_json_utf8(PERSONAL_PROVIDER_CONFIG)
        if not isinstance(config, dict):
            return None
        section = config.setdefault("config", {})
        if not isinstance(section, dict):
            return None
        section["defaultModelSelection"] = {"providerId": provider_id, "modelId": model_id}
        dump_json_utf8(PINNED_PROVIDER_CONFIG, config)
    except Exception:
        return None
    return PINNED_PROVIDER_CONFIG


def _provider_config_env() -> dict[str, str]:
    """回传需要给 CLI 子进程补的 provider 配置环境变量；无需补时返回空 dict。

    桌面 App 启动 CLI 时会把 ZCODE_BUILTIN_PROVIDER_CONFIG_FILE 指向 App 运行时缓存里那份
    （CDN 刷新过的）built-in 配置；计划任务拉起的本服务没有这个变量，CLI 就退化为按 zcode.cjs
    位置猜：`<resources>\\glm\\provider\\` 与上溯五级的 `config\\provider\\`——两处都不存在
    （实际在 `<resources>\\config\\provider\\`，只上一级），于是 skill 秒退于「无法定位 CLI
    ZCode Built-in Provider Config」，形成「手动能跑、定时不能」的分叉。

    builtin 路径含 App 版本与 endpoint 哈希，不能硬编码，故通配定位。personal 一并补上是为了
    与 App 环境完全对齐：只补 builtin 时 CLI 会多跑一次 CDN 刷新检查（实测 stderr 多一行
    「ZCode Built-in skipped (not-due)」），而那行噪声会混进失败时的 error 尾巴。

    personal 优先取 pinned_personal_config()（钉死了执行模型），拿不到时回退用户原件——降级开关是
    `INFOFLOW_SKILL_MODEL=` 置空，此时行为与钉死引入前完全一致。

    ponytail: 天花板——按 mtime 取最新一份，不校验它属于哪个 App 版本或 endpoint。已知可被自己
    污染：不带该变量裸跑 CLI（排查时的常见动作）会让 CLI 以 bundle 的占位版本 `0.0.0-dev` 物化
    一份副本，此后它 mtime 最新即被选中——内容仍是 CLI 自己认的合法副本，结果不受影响。要彻底
    消除，得按 App 版本精确匹配（版本号无处可读）或读 ZCODE_BASE_URL 反查 endpointKey（见同目录
    zcode-builtin-refresh.json）。
    """
    pinned = pinned_personal_config()
    personal = pinned or (PERSONAL_PROVIDER_CONFIG if PERSONAL_PROVIDER_CONFIG.is_file() else None)
    if os.environ.get(BUILTIN_PROVIDER_CONFIG_ENV, "").strip():
        # 已由 App 注入 builtin（本服务被 App 拉起的场景）→ 不覆盖 builtin，但仍补上钉死后的
        # personal：skill 的执行模型必须与「谁拉起服务」无关，否则 App 拉起路径会漏掉模型钉死。
        return {PERSONAL_PROVIDER_CONFIG_ENV: str(personal)} if personal else {}
    candidates = [path for path in PROVIDER_RUNTIME_ROOT.glob("*/*/endpoint-*/zcode-builtin.json") if path.is_file()]
    if not candidates:
        return {}  # 让 CLI 按自己的逻辑报错，不掩盖问题
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    env = {BUILTIN_PROVIDER_CONFIG_ENV: str(latest)}
    if personal:
        env[PERSONAL_PROVIDER_CONFIG_ENV] = str(personal)
    return env


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _tail(text: str, limit: int = ERROR_TAIL_CHARS) -> str:
    return (text or "").strip()[-limit:]


def _kill_process_tree(process: subprocess.Popen) -> None:
    """超时必须整树终止：node 会再派生 zcode 子进程，只杀父进程会留下孤儿进程。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False)
        return
    # ponytail: 非 Windows 只杀父进程；本仓部署在 Windows，跨平台需 start_new_session + killpg
    with suppress(Exception):
        process.kill()


def _run_skill_once(name: str, *, cwd: Path, timeout: int) -> dict:
    """跑一次 skill 子进程，返回 {"status", "error"}；不抛异常。"""
    node = shutil.which("node")
    cli = find_zcode_cli()
    if not node or not cli:
        return {"status": "unavailable", "error": "未找到 node 或 ZCode CLI"}

    command = [
        node,
        str(cli),
        "-p",
        SKILL_PROMPT_TEMPLATE.format(name=name),
        "--mode",
        "yolo",
        "--cwd",
        str(cwd),
        "--json",
    ]
    # Popen 也可能失败（node/CLI 被删、权限、句柄耗尽）：必须兑现「不抛异常」契约，
    # 否则异常会冲出 asyncio.to_thread → 杀死常驻调度循环。
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, **_provider_config_env()},
        )
    except OSError as exc:
        return {"status": "error", "error": f"启动 skill {name} 子进程失败：{_tail(str(exc))}"}

    try:
        _stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        with suppress(Exception):
            process.communicate(timeout=10)
        return {"status": "timeout", "error": f"skill {name} 超过 {timeout}s 未完成，已终止进程树"}

    if process.returncode == 0:
        return {"status": "success", "error": ""}
    return {"status": "error", "error": _tail(stderr) or f"skill {name} 退出码 {process.returncode}"}


def run_skill(name: str, *, cwd: Path, timeout: int = SKILL_TIMEOUT_SECONDS) -> dict:
    """同步执行一个 skill（调用方放进 asyncio.to_thread）：失败按 SKILL_RETRY_DELAYS_SECONDS 退避重试。

    只重试非零退出码——服务端掐流走这条；timeout 单次已耗满 20 分钟、unavailable 是 CLI 缺失，
    重试都不会变好，直接返回。started_at 记首次尝试时刻，finished_at 记最终返回时刻。
    """
    started_at = _now()
    attempts = 0
    outcome = {"status": "error", "error": ""}
    # 末尾的 None 是哨兵：最后一次尝试跑完即收工，不再等待
    for delay in (*SKILL_RETRY_DELAYS_SECONDS, None):
        attempts += 1
        outcome = _run_skill_once(name, cwd=cwd, timeout=timeout)
        if outcome["status"] != "error" or delay is None:
            break
        time.sleep(delay)

    error = outcome["error"]
    if outcome["status"] == "error" and attempts > 1:
        error = f"重试 {attempts - 1} 次仍失败：{error}"
    return {
        "name": name,
        "status": outcome["status"],
        "started_at": started_at,
        "finished_at": _now(),
        "error": error,
    }
"""无头执行 ZCode skill 的薄封装：发现 CLI、跑子进程、超时控制、模型故障切换。

职责单一：只负责「跑一次 skill 并回传结果」，不碰状态文件、不碰视图。
ponytail: 天花板——只判退出码，不解析 token 用量、不看产物是否其实已生成；升级路径是接入 CLI 返回的 usage 做成本统计。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from infra.utf8_json import dump_json_utf8, load_json_utf8

BASE_DIR = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)

# 严格按此顺序执行：公告清单先跑（新闻影响分析要把当天清单纳入新闻源），新闻分析后跑。
# 上一个失败/超时也继续跑下一个，避免公告失败连带新闻分析缺失。
SKILLS = ("portfolio-announcements", "portfolio-news")
# 每个 skill 的总时间预算（跨模型尝试）：75 个事件的新闻分析实测要 17~25 分钟。
# 2026-10-29 收尾超出 20 分钟被判 timeout；2026-10-30 主模型 400 后换备选，只剩 27 分钟、
# 产物 09:38 已落盘却在 09:46 被掐——45 分钟才覆盖「主模型失败 + 备选从头重跑」。
SKILL_TIMEOUT_SECONDS = 2700
# 模型/provider 硬失败（如上游 400 "The request could not be processed"）时换下一个候选，
# 最多试这么多个：2026-10-30 晨间流程同一模型连试 3 次、白等 13 分钟退避，仍然全部 400。
SKILL_MODEL_ATTEMPTS = 3
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
# 主模型 + 备用模型的默认链（逗号分隔，按顺序尝试）：2026-10-30 glm-5.3-flash 上游间歇 400，
# 主用验证过全通的 deepseek-v4.1-flash，失败再换上游 group/freedsf4.1。
SKILL_MODEL_ENV = "INFOFLOW_SKILL_MODEL"  # 覆盖模型链（逗号分隔 providerId/modelId）；置空 = 关闭钉死
DEFAULT_SKILL_MODELS = "magpie/r4coder/deepseek-v4.1-flash,magpie/group/freedsf4.1"
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


def _parse_skill_models() -> list[tuple[str, str]]:
    """把 SKILL_MODEL_ENV / DEFAULT_SKILL_MODELS 解析成 (providerId, modelId) 链。

    逗号分隔（中英文逗号都认），每项按**第一个** "/" 切分：providerId 不含 "/"，modelId 可以含
    （如 "magpie/r4coder/deepseek-v4.1-flash" → providerId="magpie"、modelId="r4coder/deepseek-v4.1-flash"）。
    未设用默认链；置空或全部条目格式非法返回空列表（= 关闭钉死，回退 App 默认模型）。
    """
    raw = os.environ.get(SKILL_MODEL_ENV)
    value = (DEFAULT_SKILL_MODELS if raw is None else raw).strip()
    entries: list[tuple[str, str]] = []
    for chunk in value.replace("，", ",").split(","):
        provider_id, sep, model_id = chunk.strip().partition("/")
        if sep and provider_id and model_id:
            entries.append((provider_id, model_id))
    return entries


def _enabled_provider_rules(section: dict) -> list[tuple[str, dict]]:
    """按 providerOrder 列出仍在用的 provider 规则（enabled 缺省视为启用）。"""
    block = section.get("providerConfigRules")
    rules = block.get("providerRules") if isinstance(block, dict) else None
    if not isinstance(rules, list):
        return []
    by_id = {rule.get("providerId"): rule for rule in rules if isinstance(rule, dict)}
    order = [pid for pid in (section.get("providerOrder") or []) if pid in by_id]
    ordered = [by_id[pid] for pid in order]
    ordered += [
        rule for rule in rules
        if isinstance(rule, dict) and rule.get("providerId") not in order
    ]
    return [(rule.get("providerId"), rule) for rule in ordered if rule.get("enabled") is not False]


def _provider_model_ids(rule: dict) -> list[str]:
    """provider 内按偏好排列的模型 id（modelOrder 优先，缺失用 personalModelIds）。"""
    conf = rule.get("config") if isinstance(rule.get("config"), dict) else {}
    ids = conf.get("modelOrder") or conf.get("personalModelIds") or []
    return [str(item) for item in ids]


def _effective_selection(section: dict, provider_id: str, model_id: str) -> dict[str, str]:
    """把钉死目标映射到配置里真正可用的 provider/model。

    用户在 App 里关掉某个 provider（或被 App 重写配置）时，CLI 会静默回退到 providerOrder
    里第一个可用 provider 的首个模型——2026-10-29 晨间流程实测跑成了慢一个数量级的推理模型，
    把 portfolio-news 拖过超时。这里显式回退：先按原意跨 provider 找同名模型，
    找不到再用首个可用 provider 的首个模型，回退打日志便于事后核对。
    """
    enabled = _enabled_provider_rules(section)
    if not enabled:
        return {"providerId": provider_id, "modelId": model_id}
    for pid, rule in enabled:
        if pid != provider_id:
            continue
        models = _provider_model_ids(rule)
        if not models or model_id in models or any(item.endswith("/" + model_id) for item in models):
            return {"providerId": provider_id, "modelId": model_id}
        logger.warning("钉死模型 %s/%s 已不在该 provider，改用 %s", model_id, provider_id, pid + "/" + models[0])
        return {"providerId": pid, "modelId": models[0]}
    for pid, rule in enabled:
        for candidate in _provider_model_ids(rule):
            if candidate == model_id or candidate.endswith("/" + model_id):
                logger.warning("钉死 provider %s 已禁用，改用 %s/%s", provider_id, pid, candidate)
                return {"providerId": pid, "modelId": candidate}
    pid, rule = enabled[0]
    candidates = _provider_model_ids(rule)
    if not candidates:
        return {"providerId": provider_id, "modelId": model_id}
    logger.warning("钉死模型 %s/%s 不可用，回退 %s/%s", provider_id, model_id, pid, candidates[0])
    return {"providerId": pid, "modelId": candidates[0]}


def _reasoning_levels(section: dict, provider_id: str, model_id: str) -> list[str]:
    """模型声明的可选推理档位（modelConfigRules 里没有该模型时返回空）。"""
    block = section.get("modelConfigRules")
    rules = block.get("providerModelRules") if isinstance(block, dict) else None
    if not isinstance(rules, list):
        return []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("providerId") != provider_id or rule.get("modelId") != model_id:
            continue
        conf = rule.get("config") if isinstance(rule.get("config"), dict) else {}
        specs = conf.get("optionSpecs") if isinstance(conf.get("optionSpecs"), dict) else {}
        level = specs.get("reasoningLevel") if isinstance(specs.get("reasoningLevel"), dict) else {}
        values = level.get("values")
        return [str(value) for value in values] if isinstance(values, list) else []
    return []


def _complete_selection(section: dict, selection: dict[str, str]) -> dict[str, object]:
    """给声明了推理档位的模型补一个档位。

    CLI 把「模型有 reasoningLevel 规格却没选档」判定为不可选，随后静默回退到注册表里
    第一个模型——2026-10-29 实测就是这样从 glm-5.3-flash 掉到 deepseek-v4.1-flash-max(xhigh)。
    技能是机械长文生成，旧 pin（custom:r4 的 flash 模型）没有推理档，故取最低档最接近原意。
    """
    levels = _reasoning_levels(section, selection["providerId"], selection["modelId"])
    if not levels:
        return selection
    level = "low" if "low" in levels else levels[0]
    return {**selection, "options": {"reasoningLevel": level}}


def _model_candidates() -> list[dict[str, object]]:
    """把声明的模型链解析成实际可用的 (provider, model) 候选，最多 SKILL_MODEL_ATTEMPTS 个。

    每个条目都过 _effective_selection（provider 被禁用/模型被移除时显式回退）和
    _complete_selection（补推理档位），并去重；链为空 = 钉死关闭，返回空列表，
    调用方沿用 App 默认模型且不做切换。
    """
    entries = _parse_skill_models()
    if not entries:
        return []
    try:
        config = load_json_utf8(PERSONAL_PROVIDER_CONFIG)
    except Exception:
        return []
    section = config.get("config") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return []
    candidates: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()
    for provider_id, model_id in entries:
        selection = _complete_selection(section, _effective_selection(section, provider_id, model_id))
        key = (selection["providerId"], selection["modelId"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(selection)
        if len(candidates) >= SKILL_MODEL_ATTEMPTS:
            break
    return candidates


def pinned_personal_config(selection: dict[str, object] | None = None) -> Path | None:
    """把 personal provider 配置的 defaultModelSelection 改成钉死模型，写一份副本并回其路径。

    模型链约定逗号分隔的 `providerId/modelId`，每项按**第一个** "/" 切分（providerId 不含 "/"，
    modelId 可含 "/"）。`selection` 为 None 时取链首（主模型）；换模型尝试时由调用方传入候选。

    未设 SKILL_MODEL_ENV 时用 DEFAULT_SKILL_MODELS；置空或全部条目非法则不钉死（返回 None）。
    钉死目标不可用（provider 被禁用）时按 _effective_selection 回退到可用模型，并给带推理档位的
    模型补档（_complete_selection）——否则 CLI 会静默忽略整条 pin，绝不留下一个死 pin。
    副本经 infra.utf8_json 原子写入 PINNED_PROVIDER_CONFIG（先临时文件再 os.replace）。
    任何异常（源读不到、JSON 坏、写失败）一律返回 None，绝不抛出、绝不阻断 skill 执行。
    副本含 personal 配置里的密钥，只落在 runtime/ 下，禁止入库或进日志。
    """
    entries = _parse_skill_models()
    if not entries:
        return None
    provider_id, model_id = entries[0]
    try:
        config = load_json_utf8(PERSONAL_PROVIDER_CONFIG)
        if not isinstance(config, dict):
            return None
        section = config.setdefault("config", {})
        if not isinstance(section, dict):
            return None
        section["defaultModelSelection"] = selection or _complete_selection(
            section, _effective_selection(section, provider_id, model_id)
        )
        dump_json_utf8(PINNED_PROVIDER_CONFIG, config)
    except Exception:
        return None
    return PINNED_PROVIDER_CONFIG


def _provider_config_env(selection: dict[str, object] | None = None) -> dict[str, str]:
    """回传需要给 CLI 子进程补的 provider 配置环境变量；无需补时返回空 dict。

    桌面 App 启动 CLI 时会把 ZCODE_BUILTIN_PROVIDER_CONFIG_FILE 指向 App 运行时缓存里那份
    （CDN 刷新过的）built-in 配置；计划任务拉起的本服务没有这个变量，CLI 就退化为按 zcode.cjs
    位置猜：`<resources>\\glm\\provider\\` 与上溯五级的 `config\\provider\\`——两处都不存在
    （实际在 `<resources>\\config\\provider\\`，只上一级），于是 skill 秒退于「无法定位 CLI
    ZCode Built-in Provider Config」，形成「手动能跑、定时不能」的分叉。

    builtin 路径含 App 版本与 endpoint 哈希，不能硬编码，故通配定位。personal 一并补上是为了
    与 App 环境完全对齐：只补 builtin 时 CLI 会多跑一次 CDN 刷新检查（实测 stderr 多一行
    「ZCode Built-in skipped (not-due)」），而那行噪声会混进失败时的 error 尾巴。

    personal 优先取 pinned_personal_config(selection)（钉死了本次尝试的模型），拿不到时回退用户
    原件——降级开关是 `INFOFLOW_SKILL_MODEL=` 置空，此时行为与钉死引入前完全一致。

    ponytail: 天花板——按 mtime 取最新一份，不校验它属于哪个 App 版本或 endpoint。已知可被自己
    污染：不带该变量裸跑 CLI（排查时的常见动作）会让 CLI 以 bundle 的占位版本 `0.0.0-dev` 物化
    一份副本，此后它 mtime 最新即被选中——内容仍是 CLI 自己认的合法副本，结果不受影响。要彻底
    消除，得按 App 版本精确匹配（版本号无处可读）或读 ZCODE_BASE_URL 反查 endpointKey（见同目录
    zcode-builtin-refresh.json）。
    """
    pinned = pinned_personal_config(selection)
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


def _run_skill_once(
    name: str, *, cwd: Path, timeout: int, selection: dict[str, object] | None = None
) -> dict:
    """按指定模型跑一次 skill 子进程，返回 {"status", "error"}；不抛异常。"""
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
            env={**os.environ, **_provider_config_env(selection)},
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
    """同步执行一个 skill（调用方放进 asyncio.to_thread），timeout 是这个 skill 的总时间预算。

    每次尝试钉一个候选模型；模型/provider 硬失败（上游 400、掐流）立即换下一个候选，
    不再原地退避——同一个坏上游等几分钟不会变好（2026-10-30 连试 3 次同一模型全 400）。
    timeout 用掉预算、unavailable 是环境缺失，都直接收工。
    """
    started_at = _now()
    deadline = time.monotonic() + timeout
    candidates: list[dict[str, object] | None] = list(_model_candidates()) or [None]
    failed: list[str] = []
    outcome = {"status": "error", "error": "没有可用的候选模型"}
    for selection in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        outcome = _run_skill_once(
            name, cwd=cwd, timeout=max(1, int(remaining)), selection=selection
        )
        if outcome["status"] == "success":
            return {
                "name": name,
                "status": "success",
                "started_at": started_at,
                "finished_at": _now(),
                "error": "",
            }
        model = f"{selection['providerId']}/{selection['modelId']}" if selection else "App 默认模型"
        failed.append(f"{model}（{outcome['status']}）")
        if outcome["status"] in ("timeout", "unavailable"):
            break
    error = outcome["error"]
    if len(failed) > 1:
        error = f"{'、'.join(failed)} 均失败：{error}"
    return {
        "name": name,
        "status": outcome["status"],
        "started_at": started_at,
        "finished_at": _now(),
        "error": error,
    }
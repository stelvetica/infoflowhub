"""无头执行 ZCode skill 的薄封装：发现 CLI、跑子进程、超时控制。

职责单一：只负责「跑一次 skill 并回传结果」，不碰状态文件、不碰视图。
ponytail: 天花板——只判退出码，不解析 token 用量、不做重试；升级路径是接入 CLI 返回的 usage 做成本统计。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import suppress
from datetime import datetime
from pathlib import Path

# 严格按此顺序执行：新闻影响分析先跑，公告清单后跑（公告产物不能因前者失败而缺失）
SKILLS = ("portfolio-news", "portfolio-announcements")
SKILL_TIMEOUT_SECONDS = 1200  # 每个 skill 20 分钟
SKILL_PROMPT_TEMPLATE = "/skill {name} 按 SKILL.md 完整执行本流程"
# 相对 %LOCALAPPDATA% 的 CLI 位置；不硬编码用户名
CLI_RELATIVE_PATH = Path("Programs") / "ZCode" / "resources" / "glm" / "zcode.cjs"
ERROR_TAIL_CHARS = 500


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


def run_skill(name: str, *, cwd: Path, timeout: int = SKILL_TIMEOUT_SECONDS) -> dict:
    """同步执行一个 skill（调用方放进 asyncio.to_thread），返回结构化结果，不抛异常。"""
    started_at = _now()
    result = {"name": name, "status": "error", "started_at": started_at, "finished_at": "", "error": ""}

    node = shutil.which("node")
    cli = find_zcode_cli()
    if not node or not cli:
        result["status"] = "unavailable"
        result["error"] = "未找到 node 或 ZCode CLI"
        result["finished_at"] = _now()
        return result

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
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        result["status"] = "error"
        result["error"] = f"启动 skill {name} 子进程失败：{_tail(str(exc))}"
        result["finished_at"] = _now()
        return result

    try:
        _stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        with suppress(Exception):
            process.communicate(timeout=10)
        result["status"] = "timeout"
        result["error"] = f"skill {name} 超过 {timeout}s 未完成，已终止进程树"
        result["finished_at"] = _now()
        return result

    result["finished_at"] = _now()
    if process.returncode == 0:
        result["status"] = "success"
        return result
    result["status"] = "error"
    result["error"] = _tail(stderr) or f"skill {name} 退出码 {process.returncode}"
    return result
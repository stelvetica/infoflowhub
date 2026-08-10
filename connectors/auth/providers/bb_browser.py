"""通过 bb-browser 从"正在运行的真实 Chrome"实时导出登录态 cookie。

背景
----
系统原登录态来源是复制常用 Chrome profile（CopyRunnerSession：抓前关 Chrome → 复制
profile → 副本抓 → 抓完重开）。该方案要关/重开 Chrome、受 ABE 影响、复制耗时长。

bb-browser（epiral/bb-browser，全局 npm 包）直连用户正在运行的真实 Chrome，可实时
按域名读出完整登录 cookie，天然按域名隔离——所有站点共用日常 Chrome 正是其设计初衷。
本模块只替代"登录态来源"这一层，抓取逻辑仍走原链路。

前置
----
- 本机已全局安装 bb-browser（`npm i -g bb-browser`），命令 `bb-browser` 可用。
- 仍需关闭 Chrome ABE（与复制方案一致，bb 复用登录态同样受 ABE 影响）。
- 用户日常 Chrome 需保持运行（bb 连真实 Chrome，不自拉实例）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

from infra.text_normalizer import normalize_utf8_text


def _bb_command() -> str:
    """定位 bb-browser 可执行文件（全局 npm bin）。"""
    resolved = shutil.which("bb-browser")
    if not resolved:
        raise RuntimeError(
            "未找到 bb-browser 命令，请先全局安装：npm i -g bb-browser"
        )
    return resolved


def _run_bb(args: list[str], *, timeout: int = 60) -> dict[str, Any]:
    """执行 bb-browser 子命令，返回解析后的 JSON dict。

    所有命令带 --json 时 stdout 为结构化 JSON（result.cookies / result.tab 等）。
    失败（daemon 未运行 / CDP 未连 / 命令报错）抛 RuntimeError 带原因。
    """
    cmd = [_bb_command(), *args, "--json"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"bb-browser 执行超时: {args[0]}") from exc
    except OSError as exc:
        raise RuntimeError(f"bb-browser 启动失败: {exc}") from exc

    stdout = normalize_utf8_text(proc.stdout or "")
    stderr = normalize_utf8_text(proc.stderr or "")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # 非 JSON（如 daemon 未跑时的提示文本）走 stderr/裸文本诊断
        msg = (stderr or stdout or "无输出").strip()
        raise RuntimeError(f"bb-browser {args[0]} 未返回 JSON: {msg}")

    # daemon 未运行/CDP 未连时，命令文本或结构里会带提示
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"bb-browser {args[0]} 报错: {payload['error']}")
    return payload


def _daemon_status() -> dict[str, Any]:
    """bb-browser daemon status 返回顶层字段（非 result 包裹）。

    兼容两种结构：顶层 {running, cdpConnected, ...} 或 {result: {...}}。
    """
    payload = _run_bb(["daemon", "status"], timeout=30)
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload


def get_cdp_endpoint() -> str:
    """确保 bb-browser daemon 连上真实 Chrome，返回其 CDP 调试端口。

    现有 DOM 抓取器（X/YouTube/抖音/小黑盒/蓝宝书）依赖 playwright page 对象。
    playwright 可 connect_over_cdp 直连 bb 连上的真实 Chrome，复用其登录态，
    替代原"复制 profile + 起副本"方案，抓取逻辑零改动。

    返回形如 http://127.0.0.1:9222 的 CDP 端点。
    """
    status = _daemon_status()
    host = str(status.get("cdpHost") or "127.0.0.1")
    port = int(status.get("cdpPort") or 0)
    if not status.get("running") or not status.get("cdpConnected") or port <= 0:
        raise RuntimeError("bb-browser 未连上真实 Chrome，无法获取 CDP 端点")
    return f"http://{host}:{port}"


def validate_daemon() -> dict[str, Any]:
    """检查 bb-browser daemon 是否运行、CDP 是否已连上真实 Chrome。

    若 daemon 未跑，尝试拉起（bb-browser daemon start）。返回状态供设置页展示。
    """
    try:
        status = _daemon_status()
        if status.get("running"):
            return {
                "is_available": True,
                "is_running": True,
                "cdp_connected": bool(status.get("cdpConnected")),
                "hint": "bb-browser daemon 运行中" + ("，已连真实 Chrome" if status.get("cdpConnected") else "，尚未连上 Chrome"),
            }
        # 未运行：尝试拉起，再确认
        _run_bb(["daemon", "start"], timeout=30)
        status = _daemon_status()
        return {
            "is_available": True,
            "is_running": bool(status.get("running")),
            "cdp_connected": bool(status.get("cdpConnected")),
            "hint": "已拉起 bb-browser daemon",
        }
    except RuntimeError as exc:
        return {
            "is_available": False,
            "is_running": False,
            "cdp_connected": False,
            "hint": f"bb-browser daemon 不可用: {exc}",
        }


def _read_cookies_for_tab(tab_id: str | None, *, timeout: int = 30) -> list[dict[str, Any]]:
    """对指定 tab 执行 cookies --json；冷启动后 open 的新 tab 可能未加载完，
    轮询重试直到读到非空 cookie 或超时（daemon 刚 start 连 CDP 时首 tab 需就绪）。"""
    deadline = time.time() + timeout
    last: list[dict[str, Any]] = []
    while time.time() < deadline:
        cookies_payload = _run_bb(
            ["cookies", "--tab", tab_id] if tab_id else ["cookies"],
            timeout=60,
        )
        result = cookies_payload.get("result") or {}
        cookies: list[dict[str, Any]] = []
        if isinstance(result, dict):
            cookies = result.get("cookies") or []
        if cookies:
            return cookies
        last = cookies
        time.sleep(0.8)
    return last


def export_cookies_from_live_chrome(
    domain_keywords: tuple[str, ...],
    *,
    open_url: str | None = None,
) -> list[dict[str, Any]]:
    """从真实 Chrome 实时导出指定域名的 cookie。

    bb-browser 连接运行中的真实 Chrome；需某站点 cookie 时，先 `open` 该站点
    域名（新开 tab 并加载），用 open 返回的新 tab 短 id 执行 `cookies --json`
    读取并按域名关键字过滤。

    注意：不能从 tab list 按 url 匹配旧 tab——同域可能积累多个 tab，旧 tab 的
    cookie 可能已过期/不全；open 新建的 tab 才保证拿到当前完整登录态。
    """
    validate_daemon()

    tab_id: str | None = None
    if open_url:
        payload = _run_bb(["open", open_url], timeout=60)
        result = payload.get("result") or {}
        if isinstance(result, dict):
            tab_id = str(result.get("tab") or "").strip() or None

    cookies = _read_cookies_for_tab(tab_id)

    if not domain_keywords:
        return cookies

    return [
        c
        for c in cookies
        if isinstance(c, dict)
        and any(str(c.get("domain") or "").find(kw) >= 0 for kw in domain_keywords)
    ]

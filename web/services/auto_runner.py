from __future__ import annotations

import asyncio
import logging
import os
import webbrowser
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from connectors._shared.chrome_runner import INFOFLOW_WEB_URL
from infra.text_normalizer import normalize_utf8_text
from web.services import views
from web.services.fetch_runtime import fetch_laterhub_now, fetch_now
from web.services.skill_runner import SKILLS, run_skill

BASE_DIR = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)

MORNING_START_HOUR = 8
MORNING_START_MINUTE = 10
CHECK_INTERVAL_SECONDS = 600
CHECK_INTERVAL_SECONDS_MORNING = 60
DT_FORMAT = "%Y-%m-%d %H:%M:%S"
# 环境变量开关的关闭取值（SSOT）：空值/未设置一律保持默认开启
_FLAG_OFF_VALUES = ("0", "false", "no", "off", "disable", "disabled")


def _env_flag_enabled(name: str, default: str = "1") -> bool:
    """解析布尔型环境变量开关：0/false/no/off/disable(d) 视为关闭，其余（含空值）开启。"""
    return os.environ.get(name, default).strip().lower() not in _FLAG_OFF_VALUES


def _flow_products(started_at: str) -> list[dict[str, Any]]:
    """扫白名单产物，mtime >= 流程开始时间即视为本次流程新增。

    复用 views 的白名单与单文件行构建（SSOT），避免另写一份 glob/时间解析。
    """
    try:
        started_ts = datetime.strptime(started_at, DT_FORMAT).timestamp()
    except ValueError:
        return []
    products: list[dict[str, Any]] = []
    for pattern, kind in views.REPORT_PATTERNS:
        for path in sorted(views.REPORTS_DIR.glob(pattern)):
            if not path.is_file() or path.stat().st_mtime < started_ts:
                continue
            row = views._build_report_row(path, kind)
            products.append({"name": row["name"], "kind": row["kind"], "is_image": row["is_image"]})
    return products


def _open_panel() -> None:
    """流程结束后把用户引到面板：产物文件不再自动打开，统一在 infoflowhub 里看。

    ponytail: 无 GUI / 无默认浏览器时 webbrowser.open 静默失败，不影响流程结果；
    真要可靠唤起，得改成显式指定浏览器可执行文件的 subprocess 调用。
    """
    if not _env_flag_enabled("INFOFLOW_OPEN_PANEL"):
        return
    with suppress(Exception):
        webbrowser.open(INFOFLOW_WEB_URL)


class AutoRunner:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        self._recover_interrupted_flow()
        self._task = asyncio.create_task(self._loop(), name="infoflowhub-auto-runner")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def _recover_interrupted_flow(self) -> None:
        """服务重启后把遗留的 running 流程改判为 error，避免面板永远显示「运行中」。"""
        state = self._load_state()
        flow = state.get("flow")
        if isinstance(flow, dict) and flow.get("status") == "running":
            flow["status"] = "error"
            flow["error"] = "服务重启，流程中断"
            flow["finished_at"] = self._format_dt(datetime.now())
            state["flow"] = flow
            self._save_state(state)

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            # 防御纵深：_maybe_run_morning 内部任一未捕获异常都不得让常驻循环退出，
            # 否则当天 last_run_date 不落盘、flow 永远停在 running、次日也不再跑。
            try:
                await self._maybe_run_morning()
            except Exception:
                logger.exception("auto-runner 调度循环单次执行失败，已跳过本轮")
            # 8 点时段用 1 分钟粒度，避免 10 分钟间隔的相位漂移把当天首次抓取推迟数分钟
            interval = (
                CHECK_INTERVAL_SECONDS_MORNING
                if datetime.now().hour == MORNING_START_HOUR
                else CHECK_INTERVAL_SECONDS
            )
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _maybe_run_morning(self) -> None:
        now = datetime.now()
        today = now.date().isoformat()

        state = self._load_state()
        last_run_date = str(state.get("last_run_date") or "")
        if last_run_date == today:
            return

        # Catch-up: if today's run was missed (process was down at the
        # 8:10 start), run as soon as the process is alive again at or after
        # the start, so a day is never silently skipped.
        if (now.hour, now.minute) < (MORNING_START_HOUR, MORNING_START_MINUTE):
            return

        await self._do_run_morning(now, today)

    async def _do_run_morning(self, now: datetime, today: str) -> None:
        async with self._lock:
            refreshed = datetime.now()
            if refreshed.date().isoformat() != today:
                return

            state = self._load_state()
            if str(state.get("last_run_date") or "") == today:
                return

            # flow.started_at 必须在抓取之前落盘，产物判定以它为时间基线
            started_at = self._format_dt(refreshed)
            state["last_started_at"] = started_at
            state["last_status"] = "running"
            state["flow"] = {
                "flow_id": refreshed.strftime("%Y%m%d-%H%M%S"),
                "started_at": started_at,
                "status": "running",
                "finished_at": "",
                "skills": [],
                "products": [],
                "error": "",
            }
            self._save_state(state)

            try:
                # 各 fetcher 内部按站点自起独立 session（每站点一个 auth profile）
                subscriptions = await asyncio.to_thread(fetch_now)
                laterhub = await asyncio.to_thread(fetch_laterhub_now)
                result = {"subscriptions": subscriptions, "laterhub": laterhub}
            except Exception as exc:
                state = self._load_state()
                error = normalize_utf8_text(str(exc))
                state["last_status"] = "error"
                state["last_finished_at"] = self._format_dt(datetime.now())
                state["last_error"] = error
                self._finalize_flow(state, started_at, status="error", error=error)
                self._save_state(state)
                await asyncio.to_thread(_open_panel)
                return

            # 抓取成功后才跑 skill；第一个 skill 失败/超时也继续跑第二个（公告产物不能缺）
            skills: list[dict[str, Any]] = []
            if self._skill_stage_enabled():
                for name in SKILLS:
                    skills.append(await asyncio.to_thread(run_skill, name, cwd=BASE_DIR))
                    # 每跑完一个就落盘：运行期状态条从 state 读 skills，否则整个
                    # 15~40 分钟运行期都误显示「阶段：抓取订阅与稍后处理」。仍在 self._lock 内。
                    self._persist_skills(skills)
            flow_status = "success" if all(item["status"] == "success" for item in skills) else "partial"

            state = self._load_state()
            state["last_run_date"] = today
            state["last_status"] = "success"
            state["last_finished_at"] = self._format_dt(datetime.now())
            state["last_error"] = ""
            state["last_result"] = result
            self._finalize_flow(state, started_at, status=flow_status, skills=skills)
            self._save_state(state)
            await asyncio.to_thread(_open_panel)

    def _persist_skills(self, skills: list[dict[str, Any]]) -> None:
        """把已完成/进行中的技能列表落盘到 flow.skills（运行期状态条据此显示阶段）。"""
        state = self._load_state()
        flow = state.get("flow") if isinstance(state.get("flow"), dict) else {}
        flow["skills"] = list(skills)
        state["flow"] = flow
        self._save_state(state)

    def _finalize_flow(
        self,
        state: dict[str, Any],
        started_at: str,
        *,
        status: str,
        skills: list[dict[str, Any]] | None = None,
        error: str = "",
    ) -> None:
        """收尾 flow：写状态/结束时间，并按 mtime 基线重算本次流程新增的产物。"""
        flow = state.get("flow") if isinstance(state.get("flow"), dict) else {}
        flow["started_at"] = started_at
        flow["finished_at"] = self._format_dt(datetime.now())
        if skills is not None:
            flow["skills"] = skills
        flow.setdefault("skills", [])
        products = _flow_products(started_at)
        flow["products"] = products
        # 技能全成功却扫不到任何新增产物：多半是 skill 产物根指向了别的仓库
        # （SKILL.md 里写死主 checkout 的 data/reports，而扫描用的是本服务 BASE_DIR）。
        # 降级为 partial 并显式报错，让面板状态条如实反映，而非显示「成功」却空产物。
        if status == "success" and skills and all(item["status"] == "success" for item in skills) and not products:
            status = "partial"
            error = "skill 执行成功但未检测到新增产物（检查产物根是否指向本服务所在仓库）"
        flow["status"] = status
        flow["error"] = error
        state["flow"] = flow

    @staticmethod
    def _skill_stage_enabled() -> bool:
        return _env_flag_enabled("INFOFLOW_SKILL_RUNNER")

    def _load_state(self) -> dict[str, Any]:
        return views.read_json(views.AUTOMATION_RUNTIME_PATH, {"last_run_date": "", "last_status": "", "last_error": ""})

    def _save_state(self, state: dict[str, Any]) -> None:
        views.write_json(views.AUTOMATION_RUNTIME_PATH, state)

    @staticmethod
    def _format_dt(value: datetime) -> str:
        return value.strftime(DT_FORMAT)
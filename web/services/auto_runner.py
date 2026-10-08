from __future__ import annotations

import asyncio
import logging
import os
import subprocess
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
# 晨间流程前置的「订阅 + 稍后读」抓取槽：7:05 只抓数据、不跑 skill，让早读更早拿到。
# 窗口 [7:05, 8:10) 内当天跑一次；过了 8:10 不补跑——晨间流程自己会抓，补一遍纯属白抓。
PREFETCH_START_HOUR = 7
PREFETCH_START_MINUTE = 5
CHECK_INTERVAL_SECONDS = 600
CHECK_INTERVAL_SECONDS_MORNING = 60
DT_FORMAT = "%Y-%m-%d %H:%M:%S"
# bsk（BrowserSkill）归本项目自己管：daemon 闲置自退，但无需 24 小时常驻保活——需要它的
# 只有晨间流程、7:05 前置抓取槽与手动「立即获取」，三者开工前都会走抓取侧 ensure_daemon()
# （用前保活，foreground 子进程拉起，见 bsk_browser._spawn_daemon_foreground）。系统层只保留
# 「登录时拉起」一条；bsk CLI 更新检查 2026-09-30 起也从系统计划任务搬进本循环，开关用
# INFOFLOW_* 惯例，默认开。
#
# 检查按天跑而非原先的每 14 天：浏览器扩展走 Chrome 自己的渠道随时静默升级，CLI 不会跟着动，
# 而 check_bsk_update.ps1 会在扩展领先时把 CLI 追平——14 天粒度会让这段错配期长达两周。
BSK_UPDATE_CHECK_INTERVAL_DAYS = 1
BSK_UPDATE_SCRIPT = BASE_DIR / "scripts" / "check_bsk_update.ps1"
BSK_UPDATE_STAMP = BASE_DIR / "runtime" / "bsk_update_last_check.txt"
# 环境变量开关的关闭取值（SSOT）：空值/未设置一律保持默认开启
_FLAG_OFF_VALUES = ("0", "false", "no", "off", "disable", "disabled")


def _env_flag_enabled(name: str, default: str = "1") -> bool:
    """解析布尔型环境变量开关：0/false/no/off/disable(d) 视为关闭，其余（含空值）开启。"""
    return os.environ.get(name, default).strip().lower() not in _FLAG_OFF_VALUES


def _fetch_all() -> dict[str, Any]:
    """抓订阅 + 稍后读（稍后读三源共用 bsk 会话，复用用户 Chrome 实时登录态，不复制 profile）。

    同步阻塞，调用方用 asyncio.to_thread 包；前置槽与晨间流程共用这一处取数（SSOT）。
    """
    return {"subscriptions": fetch_now(), "laterhub": fetch_laterhub_now()}


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
            # 防御纵深：_maybe_run_* 内部任一未捕获异常都不得让常驻循环退出，
            # 否则当天 last_run_date 不落盘、flow 永远停在 running、次日也不再跑。
            try:
                await self._maybe_run_prefetch()
            except Exception:
                logger.exception("前置抓取槽单次执行失败，已跳过本轮")
            try:
                await self._maybe_run_morning()
            except Exception:
                logger.exception("auto-runner 调度循环单次执行失败，已跳过本轮")
            # bsk 更新检查（2026-09-30 从系统计划任务搬进来）。daemon 不做常驻保活：
            # 抓取侧开工前的 ensure_daemon() 就是「用前保活」，常驻循环在 Job Object 宿主里
            # 既拉不动（bsk 拒绝 breakaway）也没必要。
            if _env_flag_enabled("INFOFLOW_BSK_UPDATE_CHECK"):
                try:
                    await asyncio.to_thread(self._maybe_check_bsk_update)
                except Exception:
                    logger.exception("bsk 更新检查失败，已跳过本轮")
            # 7 点与 8 点时段用 1 分钟粒度，避免 10 分钟间隔的相位漂移把当天首次抓取推迟数分钟
            interval = (
                CHECK_INTERVAL_SECONDS_MORNING
                if datetime.now().hour in (PREFETCH_START_HOUR, MORNING_START_HOUR)
                else CHECK_INTERVAL_SECONDS
            )
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _maybe_run_prefetch(self) -> None:
        """7:05 前置抓取槽：只抓订阅与稍后读，不跑 skill（新闻/公告仍归 8:10 晨间流程）。

        窗口 [7:05, 8:10) 内当天跑一次；过 8:10 不补跑（晨间流程自己会抓）。状态只写
        slots.prefetch，不碰 flow / last_run_date——那是晨间流程的槽位语义，前置槽不得占用
        （占用会让 8:10 的新闻流程被 last_run_date 判为「今天已跑」而整天不跑）。

        开工即落 last_attempt_date，失败当天不重试：窗口内每 60 秒 tick 一次，重试会变成
        最多 65 次的白抓；失败只降级成「今天没有前置槽」，8:10 晨间流程照常兜底。
        """
        now = datetime.now()
        if (now.hour, now.minute) < (PREFETCH_START_HOUR, PREFETCH_START_MINUTE):
            return
        if (now.hour, now.minute) >= (MORNING_START_HOUR, MORNING_START_MINUTE):
            return
        today = now.date().isoformat()
        if str(self._slot("prefetch").get("last_attempt_date") or "") == today:
            return
        await self._do_run_prefetch(today)

    async def _do_run_prefetch(self, today: str) -> None:
        async with self._lock:
            # 双检：等锁期间可能已抓过（另一 tick 抢先），或晨间流程已跑完（它自己会抓）
            if str(self._slot("prefetch").get("last_attempt_date") or "") == today:
                return
            if str(self._load_state().get("last_run_date") or "") == today:
                return

            payload: dict[str, Any] = {
                "last_attempt_date": today,
                "started_at": self._format_dt(datetime.now()),
                "finished_at": "",
                "status": "running",
                "error": "",
                "result": {},
            }
            self._write_slot("prefetch", dict(payload))
            try:
                payload["result"] = await asyncio.to_thread(_fetch_all)
                payload["status"] = "success"
            except Exception as exc:
                payload["status"] = "error"
                payload["error"] = normalize_utf8_text(str(exc))
            payload["finished_at"] = self._format_dt(datetime.now())
            self._write_slot("prefetch", payload)
            if payload["status"] == "error":
                logger.warning("前置抓取槽失败：%s", payload["error"])
            else:
                logger.info("前置抓取槽完成：%s", payload["result"])

    def _slot(self, name: str) -> dict[str, Any]:
        """读单个槽状态；缺失或形状不对一律当空槽（写入端保证形状）。"""
        slots = self._load_state().get("slots")
        slot = slots.get(name) if isinstance(slots, dict) else None
        return slot if isinstance(slot, dict) else {}

    def _write_slot(self, name: str, payload: dict[str, Any]) -> None:
        """整文件读改写，把 {name: payload} 合并进 state['slots']，其它字段原样保留。"""
        state = self._load_state()
        slots = state.get("slots")
        if not isinstance(slots, dict):
            slots = {}
        slots[name] = payload
        state["slots"] = slots
        self._save_state(state)

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

    def _maybe_check_bsk_update(self) -> None:
        """跑一次 bsk 更新检查（复用 scripts\\check_bsk_update.ps1，逻辑不重写）。

        用独立戳文件记时间，不碰 views.AUTOMATION_RUNTIME_PATH 那份共享 state——避免和 morning
        流程并发写同一个文件互相覆盖。检查失败也落戳（失败原因由脚本自己写进
        runtime\\bsk_update_check.log），否则每个 tick 都会重试一遍。
        """
        try:
            if BSK_UPDATE_STAMP.is_file():
                last = datetime.strptime(BSK_UPDATE_STAMP.read_text(encoding="utf-8").strip(), DT_FORMAT)
                if (datetime.now() - last).days < BSK_UPDATE_CHECK_INTERVAL_DAYS:
                    return
        except (OSError, ValueError):
            pass  # 戳文件缺失或损坏 → 当作「该查了」
        if not BSK_UPDATE_SCRIPT.is_file():
            logger.warning("找不到 bsk 更新检查脚本：%s", BSK_UPDATE_SCRIPT)
            return
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BSK_UPDATE_SCRIPT)],
                capture_output=True,
                timeout=180,
            )
            if result.returncode != 0:
                logger.warning("bsk 更新检查退出码 %s，详情见 runtime\\bsk_update_check.log", result.returncode)
        finally:
            BSK_UPDATE_STAMP.parent.mkdir(parents=True, exist_ok=True)
            BSK_UPDATE_STAMP.write_text(datetime.now().strftime(DT_FORMAT), encoding="utf-8")

    async def _do_run_morning(self, now: datetime, today: str) -> None:
        async with self._lock:
            refreshed = datetime.now()
            if refreshed.date().isoformat() != today:
                return

            state = self._load_state()
            if str(state.get("last_run_date") or "") == today:
                return

            # flow.started_at 必须在抓取之前落盘，产物判定以它为时间基线
            started_at = self._begin_flow(refreshed)

            try:
                result = await asyncio.to_thread(_fetch_all)
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

            skills = await asyncio.to_thread(self._run_skill_stage)
            await self._finish_flow(started_at, skills=skills, today=today, result=result)

    def _begin_flow(self, now: datetime) -> str:
        """落盘 flow=running 并返回 started_at（产物判定以该时间为基线）。"""
        state = self._load_state()
        started_at = self._format_dt(now)
        state["last_started_at"] = started_at
        state["last_status"] = "running"
        state["flow"] = {
            "flow_id": now.strftime("%Y%m%d-%H%M%S"),
            "started_at": started_at,
            "status": "running",
            "finished_at": "",
            "skills": [],
            "products": [],
            "error": "",
        }
        self._save_state(state)
        return started_at

    def _run_skill_stage(self) -> list[dict[str, Any]]:
        """抓取成功后按 SKILLS 顺序跑 skill；第一个失败/超时也继续跑第二个（公告产物不能缺）。"""
        if not self._skill_stage_enabled():
            return []
        skills: list[dict[str, Any]] = []
        for name in SKILLS:
            skills.append(run_skill(name, cwd=BASE_DIR))
            # 每跑完一个就落盘：运行期状态条从 state 读 skills，否则整个
            # 15~40 分钟运行期都误显示「阶段：抓取订阅与稍后处理」。
            self._persist_skills(skills)
        return skills

    async def _finish_flow(
        self,
        started_at: str,
        *,
        skills: list[dict[str, Any]],
        today: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """收尾一次流程：写状态/结束时间、重算产物，并打开面板。

        today/result 仅在自动流程传入；手动补跑不覆盖 last_run_date/last_result。
        """
        flow_status = "success" if all(item["status"] == "success" for item in skills) else "partial"
        state = self._load_state()
        if today is not None:
            state["last_run_date"] = today
        state["last_status"] = "success"
        state["last_finished_at"] = self._format_dt(datetime.now())
        state["last_error"] = ""
        if result is not None:
            state["last_result"] = result
        self._finalize_flow(state, started_at, status=flow_status, skills=skills)
        self._save_state(state)
        await asyncio.to_thread(_open_panel)

    async def run_skills_now(self) -> list[dict[str, Any]]:
        """手动补跑 skill 阶段：按一次完整流程登记状态，面板头部与「本次流程」据此显示。"""
        now = datetime.now()
        started_at = self._begin_flow(now)
        skills = await asyncio.to_thread(self._run_skill_stage)
        await self._finish_flow(started_at, skills=skills, today=now.date().isoformat())
        return skills

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
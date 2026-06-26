from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from infra.text_normalizer import normalize_utf8_text
from web.services.fetch_runtime import fetch_laterhub_now, fetch_now
from web.services.views import read_json, write_json

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_PATH = BASE_DIR / "runtime" / "health" / "automation_runtime.json"

MORNING_WINDOW_START = 7
MORNING_WINDOW_END = 8
CHECK_INTERVAL_SECONDS = 3600


class AutoRunner:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="infoflowhub-auto-runner")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            await self._maybe_run_morning()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=CHECK_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    async def _maybe_run_morning(self) -> None:
        now = datetime.now()
        hour = now.hour
        today = now.date().isoformat()

        state = self._load_state()
        last_run_date = str(state.get("last_run_date") or "")
        if last_run_date == today:
            return

        # Catch-up: if today's run was missed (process was down during the
        # 7-8 window), run as soon as the process is alive again at or after
        # the window start, so a day is never silently skipped.
        if hour < MORNING_WINDOW_START:
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

            state["last_run_date"] = today
            state["last_started_at"] = self._format_dt(refreshed)
            state["last_status"] = "running"
            self._save_state(state)

            try:
                # 各 fetcher 内部按站点自起独立 session（每站点一个 auth profile）
                subscriptions = await asyncio.to_thread(fetch_now)
                laterhub = await asyncio.to_thread(fetch_laterhub_now)
                result = {"subscriptions": subscriptions, "laterhub": laterhub}
            except Exception as exc:
                state = self._load_state()
                state["last_status"] = "error"
                state["last_finished_at"] = self._format_dt(datetime.now())
                state["last_error"] = normalize_utf8_text(str(exc))
                self._save_state(state)
                return

            state = self._load_state()
            state["last_status"] = "success"
            state["last_finished_at"] = self._format_dt(datetime.now())
            state["last_error"] = ""
            state["last_result"] = result
            self._save_state(state)

    def _load_state(self) -> dict[str, Any]:
        return read_json(RUNTIME_PATH, {"last_run_date": "", "last_status": "", "last_error": ""})

    def _save_state(self, state: dict[str, Any]) -> None:
        write_json(RUNTIME_PATH, state)

    @staticmethod
    def _format_dt(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

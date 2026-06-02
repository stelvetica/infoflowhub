from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Awaitable, Callable

from infra.text_normalizer import normalize_utf8_text
from web.services.fetch_runtime import fetch_laterhub_now, fetch_now
from web.services.views import read_json, write_json

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_PATH = BASE_DIR / "runtime" / "health" / "automation_runtime.json"


@dataclass(frozen=True)
class ScheduleSlot:
    key: str
    scheduled_at: time
    label: str
    runner: Callable[[], Awaitable[dict[str, Any]]]


class AutoRunner:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._slots = (
            ScheduleSlot(
                key="daily_0800",
                scheduled_at=time(hour=8, minute=0),
                label="每日 08:00 订阅+稍后读",
                runner=self._run_morning,
            ),
        )

    async def start(self) -> None:
        await self._trigger_due_slots()
        self._task = asyncio.create_task(self._loop(), name="infoflowhub-auto-runner")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            await self._trigger_due_slots()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=20)
            except TimeoutError:
                continue

    async def _trigger_due_slots(self) -> None:
        now = datetime.now()
        for slot in self._slots:
            if self._should_run(slot, now):
                await self._run_slot(slot, now)

    def _should_run(self, slot: ScheduleSlot, now: datetime) -> bool:
        state = self._load_state()
        slot_state = state.get("slots", {}).get(slot.key, {})
        today = now.date().isoformat()
        last_run_date = str(slot_state.get("last_run_date") or "")
        if last_run_date == today:
            return False
        return now >= self._scheduled_for(slot, now)

    async def _run_slot(self, slot: ScheduleSlot, now: datetime) -> None:
        async with self._lock:
            refreshed_now = datetime.now()
            if not self._should_run(slot, refreshed_now):
                return
            state = self._load_state()
            slots = state.setdefault("slots", {})
            slot_state = slots.setdefault(slot.key, {})
            slot_state["label"] = slot.label
            slot_state["last_started_at"] = self._format_dt(refreshed_now)
            slot_state["last_run_date"] = refreshed_now.date().isoformat()
            slot_state["last_status"] = "running"
            self._save_state(state)
            try:
                result = await slot.runner()
            except Exception as exc:
                state = self._load_state()
                slot_state = state.setdefault("slots", {}).setdefault(slot.key, {})
                slot_state["label"] = slot.label
                slot_state["last_status"] = "error"
                slot_state["last_finished_at"] = self._format_dt(datetime.now())
                slot_state["last_error"] = normalize_utf8_text(str(exc))
                self._save_state(state)
                return
            state = self._load_state()
            slot_state = state.setdefault("slots", {}).setdefault(slot.key, {})
            slot_state["label"] = slot.label
            slot_state["last_status"] = "success"
            slot_state["last_finished_at"] = self._format_dt(datetime.now())
            slot_state["last_error"] = ""
            slot_state["last_result"] = result
            self._save_state(state)

    async def _run_morning(self) -> dict[str, Any]:
        subscriptions = await asyncio.to_thread(fetch_now)
        laterhub = await asyncio.to_thread(fetch_laterhub_now)
        return {"subscriptions": subscriptions, "laterhub": laterhub}

    def _load_state(self) -> dict[str, Any]:
        state = read_json(RUNTIME_PATH, {"slots": {}})
        slots = state.setdefault("slots", {})
        slot_map = {slot.key: slot for slot in self._slots}
        valid_keys = set(slot_map)
        stale_keys = [key for key in list(slots.keys()) if key not in valid_keys]
        for key in stale_keys:
            slots.pop(key, None)
        for key, slot in slot_map.items():
            slot_state = slots.get(key)
            if isinstance(slot_state, dict):
                slot_state["label"] = slot.label
        if stale_keys:
            self._save_state(state)
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        write_json(RUNTIME_PATH, state)

    @staticmethod
    def _scheduled_for(slot: ScheduleSlot, now: datetime) -> datetime:
        return datetime.combine(now.date(), slot.scheduled_at)

    @staticmethod
    def _format_dt(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from apps.subscriptions.rss_db import normalize_existing_entries
from scripts.normalize_runtime_utf8 import main as normalize_runtime_utf8
from web.routes.main import router
from web.services.auto_runner import AutoRunner

BASE_DIR = Path(__file__).resolve().parents[1]


auto_runner = AutoRunner()


@asynccontextmanager
async def lifespan(_: FastAPI):
    normalize_runtime_utf8()
    normalize_existing_entries()
    await auto_runner.start()
    try:
        yield
    finally:
        await auto_runner.stop()


app = FastAPI(title="InfoFlowHub", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")
app.include_router(router)

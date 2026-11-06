from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from apps.subscriptions.rss_db import normalize_existing_entries
from scripts.normalize_runtime_utf8 import main as normalize_runtime_utf8
from web.routes.main import router
from web.services.auto_runner import AutoRunner

BASE_DIR = Path(__file__).resolve().parents[1]


auto_runner = AutoRunner()


class StaticFilesNoCache(StaticFiles):
    """静态资源强制条件校验（no-cache + ETag），浏览器不得跳过校验直接复用本地副本。

    页面 HTML 每次都新鲜，而 CSS/JS 一旦被启发式缓存沿用旧版，就会出现
    「新 HTML 配旧 CSS」：曾导致 .split-shell 的两列网格撞上旧的三列定义，
    右栏落进 20px 分割条轨道被裁成细条。本机服务重新校验只走回环，代价可忽略。
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


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
# 路由里的手动补跑要拿同一个 runner 实例（进程内单例），挂在 app.state 上避免反向 import。
app.state.auto_runner = auto_runner
app.mount("/static", StaticFilesNoCache(directory=str(BASE_DIR / "web" / "static")), name="static")
app.include_router(router)

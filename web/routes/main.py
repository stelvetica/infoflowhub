from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates

from web.services import md_render
from web.services.fetch_runtime import fetch_laterhub_now, fetch_now
from web.services.views import (
    build_entries_state_params,
    build_laterhub_state_params,
    build_news_state_params,
    delete_source,
    format_success_sources_text,
    get_laterhub_actionable_ids,
    get_entries_view,
    get_flow_state,
    get_laterhub_summary,
    get_laterhub_view,
    get_nav_settings,
    get_news_view,
    get_settings_view,
    load_status,
    mark_entry_read_state,
    mark_laterhub_opened,
    mark_laterhub_finished,
    mark_laterhub_finished_bulk,
    normalize_sources,
    save_source,
    toggle_source,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
BASE_DIR = Path(__file__).resolve().parents[2]
# 源正文目录：kind 与 web/services/views.MARKDOWN_KINDS 的取值一一对应
MARKDOWN_DIRS = {
    "alphapai": BASE_DIR / "data" / "alphapai_markdown",
    "jiuyangongshe": BASE_DIR / "data" / "jiuyangongshe_markdown",
}
REPORTS_DIR = BASE_DIR / "data" / "reports"
ENTRIES_STATE_COOKIE = "ifh_entries_state_v1"
NEWS_STATE_COOKIE = "ifh_news_state_v1"


def _resolve_inside(root: Path, name: str, suffixes: tuple[str, ...]) -> Path | None:
    """把用户传入的名字安全解析为 root 下的既有文件；越界或后缀不符返回 None。

    防目录穿越的唯一实现（/markdown、/reports/raw、/reports/view 共用）：
    先剥掉任何目录成分，带目录即判非法；再要求父目录正是 root 且目标确为文件。
    """
    basename = Path(name).name
    if not basename or basename != name:
        return None
    # 控制字符（含 NUL）会让后续 resolve/is_file 抛 ValueError 而非返回 None，
    # 调用点无捕获会变成 500；此处直接判非法，兑现「一律 404」契约。
    if any(ord(char) < 32 for char in name):
        return None
    root_resolved = root.resolve()
    try:
        target = (root_resolved / basename).resolve()
        if target.parent != root_resolved or not target.is_file():
            return None
    except (OSError, ValueError):
        return None
    if target.suffix.lower() not in suffixes:
        return None
    return target


def query_dict(request: Request) -> dict[str, str]:
    return {key: value for key, value in request.query_params.items()}


def _load_state(request: Request, cookie_key: str, builder) -> dict[str, str]:
    params = query_dict(request)
    cookie_value = request.cookies.get(cookie_key, "")
    cookie_params: dict[str, str] = {}
    if cookie_value:
        from urllib.parse import parse_qsl

        cookie_params = {key: value for key, value in parse_qsl(cookie_value, keep_blank_values=True)}
    merged = {**cookie_params, **params}
    return builder(merged)


def _persist_state(response: Response, cookie_key: str, state: dict[str, str]) -> None:
    value = urlencode({key: value for key, value in state.items() if value != ""})
    response.set_cookie(
        cookie_key,
        value=value,
        httponly=False,
        samesite="lax",
        max_age=180 * 24 * 60 * 60,
        path="/",
    )


def load_entries_state(request: Request) -> dict[str, str]:
    return _load_state(request, ENTRIES_STATE_COOKIE, build_entries_state_params)


def persist_entries_state(response: Response, state: dict[str, str]) -> None:
    _persist_state(response, ENTRIES_STATE_COOKIE, state)


def load_news_state(request: Request) -> dict[str, str]:
    return _load_state(request, NEWS_STATE_COOKIE, build_news_state_params)


def persist_news_state(response: Response, state: dict[str, str]) -> None:
    _persist_state(response, NEWS_STATE_COOKIE, state)


def merge_url(params: dict[str, str], **extra: str) -> str:
    merged = {**params, **{key: value for key, value in extra.items() if value is not None}}
    clean = {key: value for key, value in merged.items() if value != ""}
    query = urlencode(clean)
    return f"/?{query}" if query else "/"


def source_url(params: dict[str, str], **extra: str) -> str:
    merged = {**params, **extra}
    clean = {key: value for key, value in merged.items() if value != ""}
    query = urlencode({"view": "settings", **clean})
    return f"/?{query}"


def render_main(request: Request, params: dict[str, str]):
    view = params.get("view", "entries")
    entries_params = load_entries_state(request)
    news_params = load_news_state(request)
    if view != "settings":
        entries_params.setdefault("entries_unread_only", "1")
        news_params.setdefault("news_unread_only", "1")
    entries = get_entries_view(entries_params)
    news = get_news_view(news_params)
    laterhub = get_laterhub_view(params)
    laterhub_params = build_laterhub_state_params(params)
    nav_only = view != "settings"
    # 主界面不展示源统计，走轻量设置切片，避免每次加载都跑全量统计(慢)
    settings = get_nav_settings() if nav_only else get_settings_view(params)
    success_sources_text = settings["status"]["success_sources_text"]
    # source_lookup 仅设置页的源编辑弹窗使用；主界面不展示登录态，跳过 auth 校验避免首载阻塞
    source_lookup = (
        {} if nav_only else {item["id"]: item for item in normalize_sources()}
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "view": view,
            "entries": entries,
            "news": news,
            "news_params": news_params,
            "flow": get_flow_state(),
            "laterhub": laterhub,
            "laterhub_params": laterhub_params,
            "settings": settings,
            "success_sources_text": success_sources_text,
            "nav_only": nav_only,
            "params": entries_params if nav_only else params,
            "source_lookup": source_lookup,
        },
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    response = render_main(request, query_dict(request))
    persist_entries_state(response, load_entries_state(request))
    persist_news_state(response, load_news_state(request))
    return response


@router.get("/fragments/entries", response_class=HTMLResponse)
async def entries_fragment(request: Request):
    params = load_entries_state(request)
    response = templates.TemplateResponse(request, "partials/entries_table.html", {"entries": get_entries_view(params), "params": params})
    persist_entries_state(response, params)
    return response


@router.get("/fragments/news", response_class=HTMLResponse)
async def news_fragment(request: Request):
    params = load_news_state(request)
    response = templates.TemplateResponse(
        request,
        "partials/news_table.html",
        {"news": get_news_view(params), "news_params": params, "params": params, "flow": get_flow_state()},
    )
    persist_news_state(response, params)
    return response


@router.get("/fragments/news-flow-status", response_class=HTMLResponse)
async def news_flow_status_fragment(request: Request):
    """流程状态片段：运行中由片段自带 10s 轮询，终态不带触发器（空闲不产生请求）。"""
    return templates.TemplateResponse(
        request,
        "partials/news_flow_status.html",
        {"flow": get_flow_state(), "params": query_dict(request)},
    )


@router.get("/fragments/news-report/{name}", response_class=HTMLResponse)
async def news_report_fragment(request: Request, name: str):
    """新闻面板内联内容片段：md 渲染为 HTML，png 输出 img 标签；非法/缺失一律 404。

    白名单与目录穿越防护复用 _resolve_inside（与 /reports/raw、/reports/view 同一实现）。
    """
    target = _resolve_inside(REPORTS_DIR, name, (".md", ".png"))
    if target is None:
        return PlainTextResponse("Report not found", status_code=404)
    is_image = target.suffix.lower() == ".png"
    body_html = "" if is_image else md_render.render(target.read_text(encoding="utf-8", errors="replace"))
    return templates.TemplateResponse(
        request,
        "partials/news_report_content.html",
        {"title": target.stem, "name": target.name, "is_image": is_image, "body_html": body_html},
    )


@router.post("/actions/entries/mark-read")
async def mark_entry_read_action(read_key: str = Form(...)):
    return JSONResponse({"success": mark_entry_read_state(read_key)})


@router.get("/markdown/{kind}/{name}", response_class=PlainTextResponse)
async def markdown_source(kind: str, name: str):
    """订阅条目的本地正文：用 text/plain 而非 text/markdown（Chrome 无渲染器会触发下载）。"""
    root = MARKDOWN_DIRS.get(kind)
    target = _resolve_inside(root, name, (".md",)) if root else None
    if target is None:
        return PlainTextResponse("Markdown not found", status_code=404)
    return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"), media_type="text/plain; charset=utf-8")


@router.get("/reports/raw/{name}")
async def report_raw(name: str):
    """产物原始字节：.md 走 text/plain，.png 走 image/png；非法/缺失一律 404。"""
    target = _resolve_inside(REPORTS_DIR, name, (".md", ".png"))
    if target is None:
        return PlainTextResponse("Report not found", status_code=404)
    if target.suffix.lower() == ".png":
        return Response(content=target.read_bytes(), media_type="image/png")
    return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"), media_type="text/plain; charset=utf-8")


@router.get("/reports/view/{name}", response_class=HTMLResponse)
async def report_view(request: Request, name: str):
    """产物可读页：图片直接跳原始路由，markdown 渲染为 HTML。"""
    target = _resolve_inside(REPORTS_DIR, name, (".md", ".png"))
    if target is None:
        return PlainTextResponse("Report not found", status_code=404)
    if target.suffix.lower() == ".png":
        return RedirectResponse(url=f"/reports/raw/{quote(name, safe='')}", status_code=302)
    body_html = md_render.render(target.read_text(encoding="utf-8", errors="replace"))
    return templates.TemplateResponse(request, "report_view.html", {"title": target.stem, "body_html": body_html})


@router.get("/fragments/laterhub", response_class=HTMLResponse)
async def laterhub_fragment(request: Request):
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/laterhub_panel.html", {"laterhub": get_laterhub_view(params), "params": params, "laterhub_params": build_laterhub_state_params(params)})


@router.get("/fragments/runtime-status", response_class=HTMLResponse)
async def runtime_status_fragment(request: Request):
    params = query_dict(request)
    nav_only = params.get("view", "") != "settings"
    settings = get_nav_settings() if nav_only else get_settings_view(params)
    return templates.TemplateResponse(request, "partials/runtime_status.html", {"settings": settings, "params": params, "nav_only": nav_only})


@router.get("/fragments/sources", response_class=HTMLResponse)
async def sources_fragment(request: Request):
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/source_table.html", {"settings": get_settings_view(params), "params": params})


@router.get("/fragments/auth-assets", response_class=HTMLResponse)
async def auth_assets_fragment(request: Request):
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/auth_assets_panel.html", {"settings": get_settings_view(params), "params": params})


@router.get("/fragments/source-form", response_class=HTMLResponse)
async def source_form_fragment(request: Request, source_id: str = ""):
    params = query_dict(request)
    source_lookup = {item["id"]: item for item in normalize_sources()}
    draft = source_lookup.get(source_id, {"id": "", "name": "", "site_url": "", "feed_url": ""})
    return templates.TemplateResponse(request, "partials/source_modal_form.html", {"draft": draft, "params": params})


@router.post("/actions/bilibili-login/sync", response_class=HTMLResponse)
async def bilibili_login_sync(request: Request):
    from connectors.auth import invalidate_auth_status_cache
    from connectors.auth.providers import export_bilibili_cookie_from_chrome

    try:
        await run_in_threadpool(export_bilibili_cookie_from_chrome)
        invalidate_auth_status_cache()
    except Exception:
        pass
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/auth_assets_panel.html", {"settings": get_settings_view(params), "params": params})


_FETCH_BG_LOCK = threading.Lock()


def _fetch_now_background() -> None:
    """后台执行抓取。fetch_now 内部更新状态文件(fetch_state: running/success/error)。"""
    try:
        with _FETCH_BG_LOCK:
            fetch_now()
    except Exception:
        pass


@router.get("/fragments/fetch-status", response_class=JSONResponse)
async def fetch_status_fragment():
    """前端轮询抓取状态:返回 fetch_state / 后台是否执行中。"""
    return JSONResponse({
        "fetch_state": load_status().get("fetch_state", "idle"),
        "running": _FETCH_BG_LOCK.locked(),
    })


@router.post("/actions/fetch-now", response_class=HTMLResponse)
async def fetch_now_action(request: Request):
    # 若已有后台抓取在跑,不叠加新任务,仅刷新状态面板即时返回
    if not _FETCH_BG_LOCK.locked():
        worker = threading.Thread(target=_fetch_now_background, name="fetch-now-bg", daemon=True)
        worker.start()
    params = query_dict(request)
    nav_only = params.get("view", "") != "settings"
    settings = get_nav_settings() if nav_only else get_settings_view(params)
    return templates.TemplateResponse(request, "partials/runtime_status.html", {"settings": settings, "params": params, "nav_only": nav_only})


@router.post("/actions/laterhub/fetch-now", response_class=HTMLResponse)
async def laterhub_fetch_now_action(request: Request):
    await run_in_threadpool(fetch_laterhub_now)
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/laterhub_panel.html", {"laterhub": get_laterhub_view(params), "params": params, "laterhub_params": build_laterhub_state_params(params)})


@router.post("/actions/laterhub/{link_id}/toggle-finished", response_class=HTMLResponse)
async def toggle_laterhub_action(request: Request, link_id: int, finished: int = Form(...)):
    mark_laterhub_finished(link_id, bool(finished))
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/laterhub_panel.html", {"laterhub": get_laterhub_view(params), "params": params, "laterhub_params": build_laterhub_state_params(params)})


@router.post("/actions/laterhub/{link_id}/mark-opened", response_class=HTMLResponse)
async def mark_laterhub_opened_action(request: Request, link_id: int):
    mark_laterhub_opened(link_id, True)
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/laterhub_panel.html", {"laterhub": get_laterhub_view(params), "params": params, "laterhub_params": build_laterhub_state_params(params)})


@router.post("/actions/laterhub/bulk-finish", response_class=HTMLResponse)
async def bulk_finish_laterhub_action(request: Request):
    params = query_dict(request)
    actionable_ids = get_laterhub_actionable_ids(params)
    mark_laterhub_finished_bulk(actionable_ids, True)
    return templates.TemplateResponse(request, "partials/laterhub_panel.html", {"laterhub": get_laterhub_view(params), "params": params, "laterhub_params": build_laterhub_state_params(params)})


@router.post("/actions/source/save")
async def save_source_action(
    request: Request,
    source_id: str = Form(""),
    name: str = Form(...),
    site_url: str = Form(""),
    feed_url: str = Form(...),
):
    save_source({"source_id": source_id, "name": name, "site_url": site_url, "feed_url": feed_url})
    params = query_dict(request)
    return RedirectResponse(url=source_url(params, source_q=params.get("source_q", ""), source_filter=params.get("source_filter", ""), sort=params.get("sort", "name"), dir=params.get("dir", "asc")), status_code=303)


@router.post("/actions/source/{source_id}/toggle-enabled", response_class=HTMLResponse)
async def toggle_source_action(request: Request, source_id: str, enabled: int = Form(...)):
    toggle_source(source_id, bool(enabled))
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/source_table.html", {"settings": get_settings_view(params), "params": params})


@router.post("/actions/source/{source_id}/delete", response_class=HTMLResponse)
async def delete_source_action(request: Request, source_id: str):
    delete_source(source_id)
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/source_table.html", {"settings": get_settings_view(params), "params": params})

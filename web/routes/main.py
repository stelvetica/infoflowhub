from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates

from apps.laterhub.config import ATTACHMENTS_DIR, DB_PATH
from apps.laterhub.db import DBManager, IMAGE_URL_PREFIX, LinkRecord, NOTE_URL_PREFIX, is_digest_video_url
from apps.laterhub.sources import set_enabled as set_laterhub_source_enabled
from web.services import md_render
from web.services.fetch_runtime import fetch_laterhub_now, fetch_now
from web.services.views import (
    build_entries_state_params,
    build_laterhub_digest_item,
    build_laterhub_state_params,
    build_news_state_params,
    delete_source,
    format_success_sources_text,
    get_laterhub_actionable_ids,
    get_entries_view,
    get_flow_state,
    get_laterhub_summary,
    get_laterhub_view,
    get_market_gate,
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
logger = logging.getLogger(__name__)
# 手动补跑是长时间后台任务：任务引用要留住，否则可能被 GC 掉。
_SKILL_TASKS: set[asyncio.Task] = set()
BASE_DIR = Path(__file__).resolve().parents[2]
# 源正文目录：kind 与 web/services/views.MARKDOWN_KINDS 的取值一一对应
MARKDOWN_DIRS = {
    "alphapai": BASE_DIR / "data" / "alphapai_markdown",
    "jiuyangongshe": BASE_DIR / "data" / "jiuyangongshe_markdown",
}
REPORTS_DIR = BASE_DIR / "data" / "reports"
DIGEST_REPORTS_DIR = REPORTS_DIR / "video_digest"
ENTRIES_STATE_COOKIE = "ifh_entries_state_v1"
NEWS_STATE_COOKIE = "ifh_news_state_v1"
# 纯文字条目正文上限（/quick 页与后端校验共用；正文完整存 title）
NOTE_MAX_CHARS = 4000
# 图片上传上限与可识别格式。后缀只认文件头，不信客户端文件名和 Content-Type。
IMAGE_MAX_BYTES = 15 * 1024 * 1024
IMAGE_MAGIC_SUFFIXES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


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
    built = builder(merged)
    # 游标(*_after)是「本次请求」参数而非持久状态：进白名单会被写进 cookie，
    # 刷新后首屏就跳过前几批；但必须透传给视图，否则续载永远停在首批。
    return {**built, **{key: value for key, value in merged.items() if key.endswith("_after")}}


def _persist_state(response: Response, cookie_key: str, state: dict[str, str]) -> None:
    value = urlencode(
        {key: value for key, value in state.items() if value != "" and not key.endswith("_after")}
    )
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
    state = _load_state(request, NEWS_STATE_COOKIE, build_news_state_params)
    # 范围只认本次请求：历史是临时查看，不能被 cookie 钉成默认值（否则刷新后仍停在历史）
    state["news_scope"] = "all" if request.query_params.get("news_scope") == "all" else "flow"
    return state


def persist_news_state(response: Response, state: dict[str, str]) -> None:
    # 同理不把 scope 写进 cookie；其它持久状态（搜索词/排序）照旧
    _persist_state(response, NEWS_STATE_COOKIE, {**state, "news_scope": ""})


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
            # 门禁结论与 flow 分属顶层两个字段，须一并送入：状态条是 index.html/片段共用同一 include，
            # 漏传会让「局部刷新（/fragments/news）后门禁行消失」。
            "market_gate": get_market_gate(),
            "laterhub": laterhub,
            "laterhub_params": laterhub_params,
            "settings": settings,
            "success_sources_text": success_sources_text,
            "nav_only": nav_only,
            "params": entries_params if nav_only else params,
            "source_lookup": source_lookup,
        },
    )


@router.get("/quick", response_class=HTMLResponse)
async def quick_capture_page(request: Request):
    """手机端快速收录页：单屏输入，不做列表（阅读仍回主面板）。"""
    return templates.TemplateResponse(request, "quick.html", {"max_chars": NOTE_MAX_CHARS})


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
        {"news": get_news_view(params), "news_params": params, "params": params, "flow": get_flow_state(), "market_gate": get_market_gate()},
    )
    persist_news_state(response, params)
    return response


@router.get("/fragments/news-flow-status", response_class=HTMLResponse)
async def news_flow_status_fragment(request: Request):
    """流程状态片段：运行中由片段自带 10s 轮询，终态不带触发器（空闲不产生请求）。"""
    return templates.TemplateResponse(
        request,
        "partials/news_flow_status.html",
        {"flow": get_flow_state(), "market_gate": get_market_gate(), "params": query_dict(request)},
    )


async def _run_due_skills_bg(runner) -> None:  # noqa: ANN001 - 避免与 app 循环 import AutoRunner
    """后台跑当天该跑的 skill；异常只记日志，占位由 run_due_skills_now 的 finally 释放。"""
    try:
        await runner.run_due_skills_now()
    except Exception:  # noqa: BLE001
        logger.exception("手动补跑 skill 后台任务异常")


@router.post("/actions/run-skills-now", response_class=HTMLResponse)
async def run_skills_now_action(request: Request):
    """手动跑当天该跑的 skill（交易日=公告+新闻；本周最后交易日含操作分析）。

    立即返回状态片段：任务在事件循环里后台跑，避免 HTTP 请求挂十几分钟；
    刚开工时 flow 还没落 running，片段靠 force_poll 在 5s 后再拉一次，接上 10s 轮询。
    """
    runner = request.app.state.auto_runner
    started = runner.claim_manual_run()
    if started:
        task = asyncio.create_task(_run_due_skills_bg(runner), name="run-skills-now-bg")
        _SKILL_TASKS.add(task)
        task.add_done_callback(_SKILL_TASKS.discard)
    return templates.TemplateResponse(
        request,
        "partials/news_flow_status.html",
        {
            "flow": get_flow_state(),
            "market_gate": get_market_gate(),
            "params": query_dict(request),
            "force_poll": started,
        },
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


@router.get("/fragments/laterhub-sources", response_class=HTMLResponse)
async def laterhub_sources_fragment(request: Request):
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/laterhub_source_table.html", {"settings": get_settings_view(params), "params": params})


@router.post("/actions/laterhub-source/{source_id}/toggle-enabled", response_class=HTMLResponse)
async def toggle_laterhub_source_action(request: Request, source_id: str, enabled: int = Form(...)):
    # 只改启停配置，不触发抓取。
    set_laterhub_source_enabled(source_id, bool(enabled))
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/laterhub_source_table.html", {"settings": get_settings_view(params), "params": params})


@router.get("/fragments/source-form", response_class=HTMLResponse)
async def source_form_fragment(request: Request, source_id: str = ""):
    params = query_dict(request)
    source_lookup = {item["id"]: item for item in normalize_sources()}
    draft = source_lookup.get(source_id, {"id": "", "name": "", "site_url": "", "feed_url": ""})
    return templates.TemplateResponse(request, "partials/source_modal_form.html", {"draft": draft, "params": params})


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
    # 手动抓取固定走增量：深度全量会让这个 HTTP 请求挂十几分钟。
    await run_in_threadpool(fetch_laterhub_now, deep_scan=False)
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


def _digest_job(link_id: int) -> None:
    """后台跑单条视频解析。状态由 run_video_digest 回写；这里只兜底进程级异常。

    路由已先原子领取（pending/failed → running），故传 claimed=True 跳过重复 claim。
    """
    try:
        from apps.laterhub.video_digest import run_video_digest

        run_video_digest(db_path=DB_PATH, link_id=link_id, claimed=True)
    except Exception as exc:  # noqa: BLE001
        try:
            DBManager(DB_PATH).mark_digest_failed(link_id, error_message=f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass


@router.post("/actions/laterhub/{link_id}/digest", response_class=HTMLResponse)
async def start_laterhub_digest_action(request: Request, link_id: int):
    """点「解析」：原子领取后立刻返回「解析中」单元格，真正流程在后台线程跑。"""
    db = DBManager(DB_PATH)
    row = db.get_digest_row(link_id)
    if row is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    if not is_digest_video_url(row["url"]):
        raise HTTPException(status_code=400, detail="仅支持 B站/抖音视频")
    # claim 是唯一并发闸门：已 running/succeeded 时领不到，不会重复起任务
    if str(row["digest_status"]) in ("pending", "failed") and db.claim_digest(link_id, retry_failed=True):
        threading.Thread(
            target=_digest_job,
            args=(link_id,),
            name=f"laterhub-digest-{link_id}",
            daemon=True,
        ).start()
        row = db.get_digest_row(link_id)
    return templates.TemplateResponse(
        request,
        "partials/laterhub_digest_cell.html",
        {"item": build_laterhub_digest_item(dict(row))},
    )


@router.get("/fragments/laterhub/{link_id}/digest", response_class=HTMLResponse)
async def laterhub_digest_fragment(request: Request, link_id: int):
    """轮询单个条目的解析状态；终态（succeeded/failed/unsupported）由前端据此停轮询。"""
    row = DBManager(DB_PATH).get_digest_row(link_id)
    if row is None:
        return PlainTextResponse("Digest not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/laterhub_digest_cell.html",
        {"item": build_laterhub_digest_item(dict(row))},
    )


@router.get("/digest/view/{filename}", response_class=HTMLResponse)
async def digest_report_view(request: Request, filename: str):
    """解析报告：限制在 data/reports/video_digest 单目录内，防目录穿越。"""
    target = _resolve_inside(DIGEST_REPORTS_DIR, filename, (".md",))
    if target is None:
        return PlainTextResponse("Digest not found", status_code=404)
    body_html = md_render.render(target.read_text(encoding="utf-8", errors="replace"))
    return templates.TemplateResponse(request, "report_view.html", {"title": target.stem, "body_html": body_html})


@router.post("/actions/laterhub/bulk-finish", response_class=HTMLResponse)
async def bulk_finish_laterhub_action(request: Request):
    params = query_dict(request)
    actionable_ids = get_laterhub_actionable_ids(params)
    mark_laterhub_finished_bulk(actionable_ids, True)
    return templates.TemplateResponse(request, "partials/laterhub_panel.html", {"laterhub": get_laterhub_view(params), "params": params, "laterhub_params": build_laterhub_state_params(params)})


def add_laterhub_link(url: str, title: str | None) -> dict:
    """网页剪藏收链（油猴脚本/bookmarklet 等前端的唯一入口）：仅入库为未标签条目。

    打标签全权归属每日 8:10 自动流程（_tag_pending_rows 全表扫无标签行），这里不触发打标；
    source=manual_verify 复用面板已有的「人工补录」元数据。重复收录复用 upsert 语义：
    会把已完成状态翻回未完成（重新收录=重新要读）。
    """
    if not url or len(url) > 2048:
        raise ValueError("url 缺失或超长（上限 2048 字符）")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("url 必须是 http/https 链接")
    db = DBManager(DB_PATH)
    duplicated = db.find_url(url) is not None
    link_id = db.upsert_link(LinkRecord(url=url, title=(title or url)[:500], source="manual_verify"))
    return {"ok": True, "id": link_id, "duplicated": duplicated, "kind": "link"}


def add_laterhub_note(text: str) -> dict:
    """纯文字收录：正文完整存 title（面板一行省略、tooltip 看全文、搜索可命中）。

    与链接条目同池同流程（同一张表、同一轮 8:10 打标），只是 url 用内容哈希占位、天然去重。
    """
    body = text.strip()
    if not body:
        raise ValueError("文字不能为空")
    if len(body) > NOTE_MAX_CHARS:
        raise ValueError(f"文字超长（上限 {NOTE_MAX_CHARS} 字符）")
    url = f"{NOTE_URL_PREFIX}{hashlib.sha1(body.encode('utf-8')).hexdigest()[:16]}"
    db = DBManager(DB_PATH)
    duplicated = db.find_url(url) is not None
    link_id = db.upsert_link(LinkRecord(url=url, title=body, source="manual_verify"))
    return {"ok": True, "id": link_id, "duplicated": duplicated, "kind": "note"}


def _sniff_image_suffix(data: bytes) -> str | None:
    """按文件头识别图片格式；识别不了的一律拒绝（不落盘）。"""
    for magic, suffix in IMAGE_MAGIC_SUFFIXES:
        if data.startswith(magic):
            return suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_laterhub_image(data: bytes) -> dict:
    """图片收录：文件名固定为本地「日期-时间」，同秒冲突加序号。

    图片只有时间标题、没有可分类语义，直接标成已打标，不占每日打标额度。
    """
    suffix = _sniff_image_suffix(data)
    if suffix is None:
        raise ValueError("只支持 JPG、PNG、WebP、GIF 图片")
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now()
    base = stamp.strftime("%Y%m%d-%H%M%S")
    counter = 1
    while True:
        name = f"{base}{suffix}" if counter == 1 else f"{base}-{counter}{suffix}"
        try:
            # xb = 原子创建：同秒并发时由文件系统裁决，不靠先查后写
            with (ATTACHMENTS_DIR / name).open("xb") as fh:
                fh.write(data)
            break
        except FileExistsError:
            counter += 1
            if counter > 99:
                raise ValueError("同一秒内图片过多，请稍后重试")
    url = f"{IMAGE_URL_PREFIX}{name}"
    title = stamp.strftime("%Y-%m-%d %H:%M:%S")
    db = DBManager(DB_PATH)
    try:
        link_id = db.upsert_link(LinkRecord(url=url, title=title, source="manual_verify"))
    except Exception:
        (ATTACHMENTS_DIR / name).unlink(missing_ok=True)
        raise
    db.update_tags(url, "", tag_status="done")
    return {"ok": True, "id": link_id, "duplicated": False, "kind": "image", "title": title, "url": url}


@router.get("/attachments/{filename}")
async def laterhub_attachment(filename: str):
    """图片附件：只返回附件目录内的白名单后缀文件，防目录穿越与非图片内容。"""
    target = _resolve_inside(ATTACHMENTS_DIR, filename, (".jpg", ".jpeg", ".png", ".webp", ".gif"))
    if target is None:
        return PlainTextResponse("Attachment not found", status_code=404)
    return FileResponse(target)


@router.post("/actions/laterhub/add-image")
async def add_laterhub_image_action(image: UploadFile = File(...)):
    data = await image.read(IMAGE_MAX_BYTES + 1)
    if len(data) > IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"图片超过 {IMAGE_MAX_BYTES // (1024 * 1024)}MB 上限")
    try:
        return save_laterhub_image(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/actions/laterhub/add-link")
async def add_laterhub_link_action(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    url = str(payload.get("url") or "").strip()
    title = str(payload.get("title") or "").strip() or None
    text = str(payload.get("text") or "")
    try:
        if url:
            return add_laterhub_link(url, title)
        if text.strip():
            return add_laterhub_note(text)
        raise ValueError("url 或 text 至少填一个")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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

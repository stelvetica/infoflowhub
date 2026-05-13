from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web.services.fetch_runtime import fetch_now
from web.services.views import (
    delete_source,
    get_entries_view,
    get_laterhub_summary,
    get_laterhub_view,
    get_settings_view,
    mark_laterhub_finished,
    normalize_sources,
    save_source,
    toggle_source,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def query_dict(request: Request) -> dict[str, str]:
    return {key: value for key, value in request.query_params.items()}


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
    entries = get_entries_view(params)
    laterhub = get_laterhub_view(params)
    settings = get_settings_view(params)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "view": view,
            "entries": entries,
            "laterhub": laterhub,
            "settings": settings,
            "params": params,
            "laterhub_collapsed": params.get("laterhub_collapsed", "0") == "1",
            "source_lookup": {item["id"]: item for item in normalize_sources()},
        },
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return render_main(request, query_dict(request))


@router.get("/fragments/entries", response_class=HTMLResponse)
async def entries_fragment(request: Request):
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/entries_table.html", {"entries": get_entries_view(params), "params": params})


@router.get("/fragments/laterhub", response_class=HTMLResponse)
async def laterhub_fragment(request: Request):
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/laterhub_panel.html", {"laterhub": get_laterhub_view(params), "params": params})


@router.get("/fragments/runtime-status", response_class=HTMLResponse)
async def runtime_status_fragment(request: Request):
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/runtime_status.html", {"settings": get_settings_view(params), "params": params})


@router.get("/fragments/sources", response_class=HTMLResponse)
async def sources_fragment(request: Request):
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/source_table.html", {"settings": get_settings_view(params), "params": params})


@router.get("/fragments/source-form", response_class=HTMLResponse)
async def source_form_fragment(request: Request, source_id: str = ""):
    params = query_dict(request)
    source_lookup = {item["id"]: item for item in normalize_sources()}
    draft = source_lookup.get(source_id, {"id": "", "name": "", "site_url": "", "feed_url": ""})
    return templates.TemplateResponse(request, "partials/source_modal_form.html", {"draft": draft, "params": params})


@router.post("/actions/fetch-now", response_class=HTMLResponse)
async def fetch_now_action(request: Request):
    fetch_now()
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/runtime_status.html", {"settings": get_settings_view(params), "params": params})


@router.post("/actions/laterhub/{link_id}/toggle-finished", response_class=HTMLResponse)
async def toggle_laterhub_action(request: Request, link_id: int, finished: int = Form(...)):
    mark_laterhub_finished(link_id, bool(finished))
    params = query_dict(request)
    return templates.TemplateResponse(request, "partials/laterhub_panel.html", {"laterhub": get_laterhub_view(params), "params": params})


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

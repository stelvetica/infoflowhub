from __future__ import annotations

import json
from functools import cmp_to_key
from pathlib import Path
from typing import Any

from apps.subscriptions.config import load_settings, load_sources, save_sources
from apps.subscriptions.models import SourceItem
from apps.subscriptions.rss_db import (
    delete_entries_by_source,
    delete_source_state,
    get_connection,
    list_source_stats,
    rename_source,
    sanitize_db_text,
)
from connectors._shared.common import parse_published_datetime, resolve_web_target
from connectors._shared.web_fetch import validate_x_login_prerequisite
from connectors.wechat.auth import validate_wechat_auth_prerequisite

from web.services.utils import (
    build_source_id,
    compare_value,
    format_date,
    format_datetime,
    join_tags,
    normalize_text,
    provider_label,
    source_channel_label,
    split_tags,
    strip_invalid_unicode,
    to_sortable_time,
)

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"
HEALTH_PATH = RUNTIME_DIR / "health" / "subscriptions_source_health.json"
STATUS_PATH = RUNTIME_DIR / "health" / "subscriptions_status.json"
LATERHUB_DB_PATH = BASE_DIR / "data" / "laterhub.sqlite3"

DELETED_SITE_URLS = {"https://www.huxiu.com/member/2321131.html"}
ENTRIES_PAGE_SIZE = 35
LATERHUB_PAGE_SIZE = 18


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_status() -> dict[str, Any]:
    return read_json(
        STATUS_PATH,
        {
            "fetch_state": "idle",
            "current_run_started_at": "",
            "last_run_at": "",
            "last_success_at": "",
            "last_error": "",
            "last_total_sources": 0,
            "last_success_sources": 0,
            "last_inserted_entries": 0,
        },
    )


def load_health() -> dict[str, Any]:
    return read_json(HEALTH_PATH, {"sources": {}})


def get_login_requirement_meta(source: dict[str, Any]) -> dict[str, str] | None:
    if str(source.get("auth_type") or "").strip().lower() == "wechat_session":
        hint = validate_wechat_auth_prerequisite(source)
        if not hint:
            hint = "请在环境变量或 runtime/wechat_auth.json 中维护 WECHAT_TOKEN 与 WECHAT_COOKIE。"
        return {"requirement": "依赖微信公众号登录态", "hint": hint}
    if str(source.get("auth_type") or "").strip().lower() == "chrome_profile_x":
        requirement = "依赖本机 Chrome Profile 2 登录态"
        hint = validate_x_login_prerequisite(source)
        if not hint:
            hint = "请先在本机 Chrome 的 Profile 2 中登录 x.com，并确认 MacroMargin 时间线可正常加载。"
        return {"requirement": requirement, "hint": hint}
    return None


def infer_source_meta(feed_url: str, site_url: str) -> tuple[str, str, str]:
    feed = normalize_text(feed_url)
    site = normalize_text(site_url)
    combined = f"{feed} {site}"
    if "wechat://mp/" in combined or "mp.weixin.qq.com" in combined:
        return ("web", "wechat-api", "web")
    if any(host in combined for host in ("bilibili.com", "x.com", "twitter.com", "weibo.com", "douyin.com")):
        return ("web", "web", "web")
    if "rsshub" in feed:
        return ("rsshub", "rsshub-self-hosted", "rsshub")
    return ("native", "direct", "native")


def infer_channel(feed_url: str, site_url: str) -> str:
    target = resolve_web_target({"feed_url": feed_url, "site_url": site_url})
    if target:
        return target.site
    combined = f"{normalize_text(feed_url)} {normalize_text(site_url)}"
    if "wechat://mp/" in combined or "mp.weixin.qq.com" in combined:
        return "wechat"
    if "youtube.com" in combined or "youtu.be" in combined:
        return "youtube"
    if "rsshub" in combined:
        return "rsshub"
    return "rss"


def canonicalize_source(item: dict[str, Any]) -> dict[str, Any] | None:
    feed_url = str(item.get("feed_url") or "").strip()
    site_url = str(item.get("site_url") or "").strip()
    if not feed_url or site_url in DELETED_SITE_URLS:
        return None
    provider = str(item.get("provider") or "").strip()
    fetch_via = str(item.get("fetch_via") or "").strip()
    kind = str(item.get("kind") or "").strip()
    if not provider or not fetch_via or not kind:
        provider, fetch_via, kind = infer_source_meta(feed_url, site_url)
    channel = str(item.get("channel") or "").strip() or infer_channel(feed_url, site_url)
    auth_type = str(item.get("auth_type") or "").strip() or ("wechat_session" if channel == "wechat" else ("chrome_profile_x" if normalize_text(site_url) == "https://x.com/macromargin" else "none"))
    auth_profile = str(item.get("auth_profile") or "").strip() or ("runtime/wechat_auth.json" if auth_type == "wechat_session" else ("Profile 2" if auth_type == "chrome_profile_x" else ""))
    fallback_mode = str(item.get("fallback_mode") or "").strip()
    if not fallback_mode:
        fallback_mode = "web" if channel == "youtube" else "none"
    source_id = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    if not source_id or not name:
        return None
    return {
        "id": source_id,
        "name": name,
        "group": str(item.get("group") or "").strip(),
        "feed_url": feed_url,
        "site_url": site_url,
        "provider": provider,
        "fetch_via": fetch_via,
        "kind": kind,
        "enabled": bool(item.get("enabled", True)),
        "note": str(item.get("note") or ""),
        "channel": channel,
        "auth_type": auth_type,
        "auth_profile": auth_profile,
        "fallback_mode": fallback_mode,
    }


def load_source_catalog() -> list[dict[str, Any]]:
    payload = {"sources": load_sources()}
    sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_feeds: set[str] = set()
    for item in payload["sources"]:
        source = canonicalize_source(item)
        if not source:
            continue
        if source["id"] in seen_ids or source["feed_url"] in seen_feeds:
            continue
        seen_ids.add(source["id"])
        seen_feeds.add(source["feed_url"])
        sources.append(source)
    return sources


def normalize_sources() -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in load_source_catalog():
        source = dict(item)
        login_meta = get_login_requirement_meta(source)
        if login_meta:
            source["login_requirement"] = login_meta["requirement"]
            source["login_hint"] = login_meta["hint"]
        normalized.append(source)
    return normalized


def save_source(payload: dict[str, str]) -> None:
    raw_sources = [canonicalize_source(item) for item in load_sources()]
    raw_sources = [item for item in raw_sources if item]
    sources = load_source_catalog()
    existing = next((item for item in sources if item["id"] == payload.get("source_id", "").strip()), None)
    previous_name = str(existing.get("name") or "").strip() if existing else ""
    clean_feed_url = sanitize_db_text(payload["feed_url"]).strip()
    clean_site_url = sanitize_db_text(payload.get("site_url", "")).strip()
    clean_name = sanitize_db_text(payload["name"]).strip()
    requested_source_id = sanitize_db_text(payload.get("source_id", "")).strip()
    provider, fetch_via, kind = infer_source_meta(clean_feed_url, clean_site_url)
    channel = infer_channel(clean_feed_url, clean_site_url)
    auth_type = str(existing.get("auth_type") or "").strip() if existing else ("wechat_session" if channel == "wechat" else ("chrome_profile_x" if normalize_text(clean_site_url) == "https://x.com/macromargin" else "none"))
    auth_profile = str(existing.get("auth_profile") or "").strip() if existing else ("runtime/wechat_auth.json" if auth_type == "wechat_session" else ("Profile 2" if auth_type == "chrome_profile_x" else ""))
    fallback_mode = str(existing.get("fallback_mode") or "").strip() if existing else ("web" if channel == "youtube" else "none")
    if not existing:
        source_id = requested_source_id
        if not source_id:
            source_id = build_source_id(clean_name)
    else:
        source_id = existing["id"]
    target = {
        "id": source_id,
        "name": clean_name,
        "group": str(existing.get("group") or "").strip() if existing else "",
        "feed_url": clean_feed_url,
        "site_url": clean_site_url,
        "provider": provider,
        "fetch_via": fetch_via,
        "kind": kind,
        "enabled": existing["enabled"] if existing else True,
        "note": str(existing.get("note") or "") if existing else "",
        "channel": channel,
        "auth_type": auth_type,
        "auth_profile": auth_profile,
        "fallback_mode": fallback_mode,
    }
    if not target["name"] or not target["feed_url"]:
        return
    if existing:
        next_sources = [target if str(item.get("id") or "").strip() == target["id"] else item for item in raw_sources]
    else:
        next_sources = [*raw_sources, target]
    save_sources(next_sources)
    if existing and previous_name != target["name"]:
        rename_source(target["id"], target["name"])


def toggle_source(source_id: str, enabled: bool) -> None:
    clean_id = sanitize_db_text(source_id).strip()
    if not clean_id:
        return
    raw_sources = load_sources()
    next_sources: list[dict[str, Any]] = []
    for item in raw_sources:
        current_id = str(item.get("id") or "").strip()
        if current_id == clean_id:
            next_sources.append({**item, "enabled": enabled})
        else:
            next_sources.append(item)
    save_sources(next_sources)


def delete_source(source_id: str) -> None:
    clean_id = sanitize_db_text(source_id).strip()
    if not clean_id:
        return
    save_sources([item for item in load_sources() if str(item.get("id") or "").strip() != clean_id])
    delete_entries_by_source(clean_id)
    delete_source_state(clean_id)
    health = load_health()
    if health["sources"].get(clean_id):
        del health["sources"][clean_id]
        write_json(HEALTH_PATH, health)


def _load_entries(limit: int = 500) -> list[dict[str, Any]]:
    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            """
            SELECT source_id, source_name, title, link, published, published_at, summary, created_at
            FROM rss_entries
            ORDER BY COALESCE(NULLIF(published_at, ''), NULLIF(published, ''), created_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _count_entries() -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) FROM rss_entries").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _laterhub_conn():
    import sqlite3
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _load_laterhub_items(limit: int | None = None) -> list[dict[str, Any]]:
    if not LATERHUB_DB_PATH.exists():
        return []
    conn = _laterhub_conn()
    try:
        if limit is None:
            rows = conn.execute(
                """
                SELECT id, url, title, tags, created_at, updated_at, is_finished
                FROM links
                ORDER BY id DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, url, title, tags, created_at, updated_at, is_finished
                FROM links
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def count_laterhub_items() -> int:
    if not LATERHUB_DB_PATH.exists():
        return 0
    conn = _laterhub_conn()
    try:
        row = conn.execute("SELECT COUNT(*) FROM links").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def get_laterhub_summary() -> dict[str, int]:
    if not LATERHUB_DB_PATH.exists():
        return {"total_count": 0, "unfinished_count": 0, "finished_count": 0}
    conn = _laterhub_conn()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total_count,
                   SUM(CASE WHEN is_finished = 1 THEN 1 ELSE 0 END) AS finished_count,
                   SUM(CASE WHEN is_finished = 0 THEN 1 ELSE 0 END) AS unfinished_count
            FROM links
            """
        ).fetchone()
    finally:
        conn.close()
    return {
        "total_count": int(row["total_count"] or 0),
        "unfinished_count": int(row["unfinished_count"] or 0),
        "finished_count": int(row["finished_count"] or 0),
    }


def laterhub_source_meta(source: str) -> dict[str, str]:
    mapping = {
        "bilibili_watchlater": {"label": "B站稍后看", "purpose": "收藏/稍后处理", "fetch_mode": "私有 API 登录态"},
        "douyin_favorite": {"label": "抖音收藏", "purpose": "收藏/稍后处理", "fetch_mode": "私有网页登录态"},
        "manual_verify": {"label": "人工补录", "purpose": "资料核实/待整理", "fetch_mode": "人工录入"},
    }
    return mapping.get(source, {"label": source, "purpose": "待分类", "fetch_mode": "待识别"})


def get_laterhub_source_stats() -> list[dict[str, Any]]:
    if not LATERHUB_DB_PATH.exists():
        return []
    conn = _laterhub_conn()
    try:
        rows = conn.execute(
            """
            SELECT source,
                   COUNT(*) AS total_count,
                   SUM(CASE WHEN is_finished = 1 THEN 1 ELSE 0 END) AS finished_count,
                   SUM(CASE WHEN is_finished = 0 THEN 1 ELSE 0 END) AS unfinished_count
            FROM links
            GROUP BY source
            ORDER BY source COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.update(laterhub_source_meta(item["source"]))
        result.append(item)
    return result


def mark_laterhub_finished(link_id: int, finished: bool) -> None:
    if not LATERHUB_DB_PATH.exists():
        return
    conn = _laterhub_conn()
    try:
        from datetime import datetime

        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            UPDATE links
            SET is_finished = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if finished else 0, now_text if finished else None, now_text, link_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_entries_view(query: dict[str, str]) -> dict[str, Any]:
    enabled_ids = {item["id"] for item in normalize_sources() if item.get("enabled")}
    keyword = normalize_text(query.get("entries_q", ""))
    sort = query.get("entries_sort") or "sort_time"
    direction = query.get("entries_dir") or "desc"
    page = max(int(query.get("entries_page", "1") or "1"), 1)
    rows = []
    for item in _load_entries(500):
        if item["source_id"] not in enabled_ids:
            continue
        if keyword and not any(keyword in normalize_text(str(item.get(field) or "")) for field in ("source_name", "title", "summary")):
            continue
        row = {
            **item,
            "display_time": format_datetime(item.get("published_at") or item.get("published") or item.get("created_at") or ""),
            "sort_time": to_sortable_time(item.get("published_at") or item.get("published") or item.get("created_at") or ""),
        }
        rows.append(row)
    rows.sort(key=cmp_to_key(lambda a, b: compare_value(a.get(sort), b.get(sort), direction)))
    total = len(rows)
    total_pages = max((total + ENTRIES_PAGE_SIZE - 1) // ENTRIES_PAGE_SIZE, 1)
    safe_page = min(page, total_pages)
    page_rows = rows[(safe_page - 1) * ENTRIES_PAGE_SIZE : safe_page * ENTRIES_PAGE_SIZE]
    return {
        "rows": page_rows,
        "sort": sort,
        "dir": direction,
        "q": query.get("entries_q", ""),
        "page": safe_page,
        "page_size": ENTRIES_PAGE_SIZE,
        "total_pages": total_pages,
        "filtered_total": total,
        "total": _count_entries(),
    }


def get_laterhub_view(query: dict[str, str]) -> dict[str, Any]:
    sort = query.get("laterhub_sort") or "sort_time"
    direction = query.get("laterhub_dir") or "desc"
    keyword = normalize_text(query.get("laterhub_q", ""))
    filter_finished = query.get("laterhub_filter_finished", "0")
    selected_tags = split_tags(query.get("laterhub_filter_tag", ""))
    selected_keys = {normalize_text(item) for item in selected_tags}
    page = max(int(query.get("laterhub_page", "1") or "1"), 1)
    all_rows: list[dict[str, Any]] = []
    for item in _load_laterhub_items():
        tags_text = str(item.get("tags") or "")
        tag_list = split_tags(tags_text)
        all_rows.append(
            {
                **item,
                "display_time": format_date(item.get("created_at", "")),
                "sort_time": to_sortable_time(item.get("created_at", "")),
                "tags_text": tags_text,
                "tag_list": tag_list,
                "tag_keys": {normalize_text(tag) for tag in tag_list},
            }
        )
    rows = []
    for item in all_rows:
        if keyword and keyword not in normalize_text(item["title"]) and keyword not in normalize_text(item["tags_text"]):
            continue
        if filter_finished == "1" and not item["is_finished"]:
            continue
        if filter_finished == "0" and item["is_finished"]:
            continue
        if not selected_keys.issubset(item["tag_keys"]):
            continue
        rows.append(item)
    rows.sort(key=cmp_to_key(lambda a, b: compare_value(a.get(sort), b.get(sort), direction)))
    all_tags_map: dict[str, str] = {}
    for item in all_rows:
        for tag in item["tag_list"]:
            key = normalize_text(tag)
            if key not in all_tags_map:
                all_tags_map[key] = tag
    all_tags = [all_tags_map[key] for key in sorted(all_tags_map.keys())]
    total_pages = max((len(rows) + LATERHUB_PAGE_SIZE - 1) // LATERHUB_PAGE_SIZE, 1)
    safe_page = min(page, total_pages)
    page_rows = rows[(safe_page - 1) * LATERHUB_PAGE_SIZE : safe_page * LATERHUB_PAGE_SIZE]
    return {
        "rows": page_rows,
        "total": count_laterhub_items(),
        "filtered_total": len(rows),
        "all_tags": all_tags,
        "selected_tags": selected_tags,
        "selected_tags_text": join_tags(selected_tags),
        "sort": sort,
        "dir": direction,
        "q": query.get("laterhub_q", ""),
        "filter_finished": filter_finished,
        "page": safe_page,
        "page_size": LATERHUB_PAGE_SIZE,
        "total_pages": total_pages,
    }


def get_settings_view(query: dict[str, str]) -> dict[str, Any]:
    status = load_status()
    summary = get_laterhub_summary()
    laterhub_sources = get_laterhub_source_stats()
    health_sources = load_health().get("sources", {})
    stats = {item["source_id"]: item for item in list_source_stats()}
    keyword = normalize_text(query.get("source_q", ""))
    source_filter = query.get("source_filter", "")
    sort = query.get("sort", "name")
    direction = query.get("dir", "asc")
    rows = []
    for item in normalize_sources():
        stat = stats.get(item["id"], {})
        source_health = health_sources.get(item["id"], {})
        failed_at = parse_published_datetime(source_health.get("last_failed_at", "") or "")
        success_at = parse_published_datetime(source_health.get("last_success_at", "") or "")
        invalid_days = ""
        if failed_at and (not success_at or failed_at > success_at):
            from datetime import datetime

            invalid_days = str(max((datetime.now() - failed_at).days, 0))
        row = {
            **item,
            "provider_label": provider_label(str(item.get("provider") or ""), str(item.get("fetch_via") or "")),
            "channel_label": source_channel_label(str(item.get("feed_url") or ""), str(item.get("site_url") or ""), str(item.get("provider") or "")),
            "entry_count": int(stat.get("entry_count") or 0),
            "last_error": str(source_health.get("last_error") or ""),
            "last_success_at": str(source_health.get("last_success_at") or ""),
            "login_requirement": str(item.get("login_requirement") or ""),
            "login_hint": str(item.get("login_hint") or ""),
            "invalid_days": invalid_days,
            "is_failed": bool(invalid_days),
            "invalid_sort": int(invalid_days) if invalid_days else -1,
            "enabled_sort": 1 if item.get("enabled") else 0,
            "enabled_text": "生效" if item.get("enabled") else "停用",
        }
        if keyword and not any(keyword in normalize_text(str(row.get(field) or "")) for field in ("name", "feed_url", "site_url")):
            continue
        if source_filter == "failed" and not row["is_failed"]:
            continue
        rows.append(row)
    rows.sort(key=cmp_to_key(lambda a, b: compare_value(a.get(sort), b.get(sort), direction)))
    return {
        "status": status,
        "summary": summary,
        "laterhub_sources": laterhub_sources,
        "sources": rows,
        "q": query.get("source_q", ""),
        "source_filter": source_filter,
        "sort": sort,
        "dir": direction,
        "settings_raw": load_settings(),
    }

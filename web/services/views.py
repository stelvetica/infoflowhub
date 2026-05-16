from __future__ import annotations

import json
import re
from datetime import datetime
from functools import cmp_to_key
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.parse import quote

from apps.subscriptions.config import load_settings, load_sources, save_sources
from apps.subscriptions.rss_db import (
    delete_entries_by_source,
    delete_source_state,
    get_connection,
    list_source_stats,
    rename_source,
    sanitize_db_text,
)
from connectors._shared.common import parse_published_datetime, resolve_web_target
from connectors.auth import list_auth_statuses
from connectors.wechat.auth import get_wechat_auth_status
from infra.utf8_json import dump_json_utf8, load_json_utf8
from web.services.utils import (
    build_source_id,
    compare_value,
    format_date,
    format_datetime,
    join_tags,
    normalize_text,
    read_link_key,
    provider_label,
    source_channel_label,
    split_tags,
    to_sortable_time,
)

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"
HEALTH_PATH = RUNTIME_DIR / "health" / "subscriptions_source_health.json"
STATUS_PATH = RUNTIME_DIR / "health" / "subscriptions_status.json"
LATERHUB_DB_PATH = BASE_DIR / "data" / "laterhub.sqlite3"
WEB_LOG_PATH = RUNTIME_DIR / "web.log"

DELETED_SITE_URLS = {"https://www.huxiu.com/member/2321131.html"}
ENTRIES_PAGE_SIZE = 20
LATERHUB_PAGE_SIZE = 10
ENTRIES_READ_CUTOFF = datetime(2026, 5, 1).timestamp()
ABSOLUTE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
BILIBILI_DYNAMIC_PATH_RE = re.compile(r"^/(\d+)$")


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return load_json_utf8(path)
    except Exception:
        return fallback


def write_json(path: Path, value: Any) -> None:
    dump_json_utf8(path, value)


def read_log_tail(path: Path, max_lines: int = 80) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    tail = [line for line in lines[-max_lines:] if line.strip()]
    return "\n".join(tail)


def normalize_entry_link(link: str, fallback: str = "") -> str:
    primary = str(link or "").strip()
    if ABSOLUTE_URL_RE.match(primary):
        parsed = urlparse(primary)
        if parsed.netloc.lower() == "t.bilibili.com":
            matched = BILIBILI_DYNAMIC_PATH_RE.match(parsed.path or "")
            if matched:
                return f"https://www.bilibili.com/opus/{matched.group(1)}"
        return primary
    backup = str(fallback or "").strip()
    if ABSOLUTE_URL_RE.match(backup):
        return backup
    return ""


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


def format_success_sources_text(status: dict[str, Any]) -> str:
    return f"{int(status.get('last_success_sources') or 0)}/{int(status.get('last_total_sources') or 0)}"


def load_health() -> dict[str, Any]:
    return read_json(HEALTH_PATH, {"sources": {}})


def default_auth_key(channel: str, site_url: str, feed_url: str = "") -> str:
    normalized_site = normalize_text(site_url)
    normalized_feed = normalize_text(feed_url)
    combined = f"{normalized_site} {normalized_feed}"
    if channel == "wechat":
        return "wechat_mp_main"
    if "bilibili.com" in combined:
        return "bilibili_main"
    if normalized_site == "https://x.com/macromargin" or "x.com/" in combined:
        return "x_profile2"
    if "weibo.com/" in combined:
        return "weibo_shared"
    if channel == "douyin":
        return "douyin_shared"
    return ""


def auth_requirement_meta(auth_key: str) -> dict[str, str] | None:
    for descriptor in list_auth_statuses():
        if descriptor.auth_key != auth_key:
            continue
        requirement_map = {
            "wechat_mp_main": "依赖微信公众号主账号登录态",
            "douyin_shared": "依赖抖音共享登录态",
            "bilibili_main": "依赖 B站主账号登录态",
            "x_profile2": "依赖 X 平台共享登录态",
            "weibo_shared": "依赖微博共享登录态",
        }
        return {
            "requirement": requirement_map.get(auth_key, descriptor.display_name),
            "hint": descriptor.hint,
            "status_text": descriptor.status_text,
            "status_level": descriptor.status_level,
        }
    return None


def infer_source_meta(feed_url: str, site_url: str) -> tuple[str, str, str]:
    feed = normalize_text(feed_url)
    site = normalize_text(site_url)
    combined = f"{feed} {site}"
    if "wechat://mp/" in combined or "mp.weixin.qq.com" in combined:
        return ("web", "wechat-api", "web")
    if any(host in combined for host in ("bilibili.com", "x.com", "twitter.com", "weibo.com", "douyin.com", "youtube.com", "youtu.be")):
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
    source_id = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    feed_url = str(item.get("feed_url") or "").strip()
    site_url = str(item.get("site_url") or "").strip()
    provider = str(item.get("provider") or "").strip()
    fetch_via = str(item.get("fetch_via") or "").strip()
    kind = str(item.get("kind") or "").strip()
    channel = str(item.get("channel") or "").strip()
    auth_key = str(item.get("auth_key") or "").strip()
    fallback_mode = str(item.get("fallback_mode") or "").strip()
    if not all((source_id, name, feed_url, provider, fetch_via, kind, channel, fallback_mode)):
        return None
    if not feed_url or site_url in DELETED_SITE_URLS:
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
        "auth_key": auth_key,
        "fallback_mode": fallback_mode,
    }


def load_source_catalog() -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_feeds: set[str] = set()
    for item in load_sources():
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
        login_meta = auth_requirement_meta(source.get("auth_key", ""))
        if login_meta:
            source["login_requirement"] = login_meta["requirement"]
            source["login_hint"] = login_meta["hint"]
            source["auth_status_text"] = login_meta["status_text"]
            source["auth_status_level"] = login_meta["status_level"]
        else:
            source["login_requirement"] = ""
            source["login_hint"] = ""
            source["auth_status_text"] = ""
            source["auth_status_level"] = ""
        normalized.append(source)
    return normalized


def source_runtime_health(source_id: str) -> dict[str, str]:
    source_health = load_health().get("sources", {}).get(source_id, {})
    return {
        "last_checked_at": str(source_health.get("last_checked_at") or ""),
        "last_success_at": str(source_health.get("last_success_at") or ""),
        "last_failed_at": str(source_health.get("last_failed_at") or ""),
        "last_error": str(source_health.get("last_error") or ""),
    }


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
    auth_key = str(existing.get("auth_key") or "").strip() if existing else default_auth_key(channel, clean_site_url, clean_feed_url)
    fallback_mode = str(existing.get("fallback_mode") or "").strip() if existing else ("web" if channel == "youtube" else "none")
    source_id = existing["id"] if existing else (requested_source_id or build_source_id(clean_name))
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
        "auth_key": auth_key,
        "fallback_mode": fallback_mode,
    }
    if not target["name"] or not target["feed_url"]:
        return
    next_sources = [target if str(item.get("id") or "").strip() == target["id"] else item for item in raw_sources] if existing else [*raw_sources, target]
    save_sources(next_sources)
    if existing and previous_name != target["name"]:
        rename_source(target["id"], target["name"])


def toggle_source(source_id: str, enabled: bool) -> None:
    clean_id = sanitize_db_text(source_id).strip()
    if not clean_id:
        return
    next_sources: list[dict[str, Any]] = []
    for item in load_sources():
        current_id = str(item.get("id") or "").strip()
        next_sources.append({**item, "enabled": enabled} if current_id == clean_id else item)
    save_sources(next_sources)


def delete_source(source_id: str) -> None:
    clean_id = sanitize_db_text(source_id).strip()
    if not clean_id:
        return
    save_sources([item for item in load_sources() if str(item.get("id") or "").strip() != clean_id])
    delete_entries_by_source(clean_id)
    delete_source_state(clean_id)
    health = load_health()
    if health.get("sources", {}).get(clean_id):
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
        "bilibili_watchlater": {"label": "B站稍后看", "purpose": "收藏/稍后处理", "fetch_mode": "统一复用 bilibili_main", "auth_key": "bilibili_main"},
        "douyin_favorite": {"label": "抖音收藏", "purpose": "收藏/稍后处理", "fetch_mode": "统一复用 douyin_shared", "auth_key": "douyin_shared"},
        "manual_verify": {"label": "人工补录", "purpose": "资料核实/待整理", "fetch_mode": "人工录入", "auth_key": ""},
    }
    return mapping.get(source, {"label": source, "purpose": "待分类", "fetch_mode": "待识别", "auth_key": ""})


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
    current_sources = normalize_sources()
    enabled_ids = {item["id"] for item in current_sources if item.get("enabled")}
    current_name_map = {item["id"]: str(item.get("name") or "").strip() for item in current_sources}
    current_site_map = {item["id"]: str(item.get("site_url") or "").strip() for item in current_sources}
    keyword = normalize_text(query.get("entries_q", ""))
    unread_only = query.get("entries_unread_only", "0") == "1"
    read_keys = {item for item in split_tags(query.get("entries_read_keys", "")) if item}
    sort = query.get("entries_sort") or "sort_time"
    direction = query.get("entries_dir") or "desc"
    page = max(int(query.get("entries_page", "1") or "1"), 1)
    rows = []
    for item in _load_entries(500):
        if item["source_id"] not in enabled_ids:
            continue
        if keyword and not any(keyword in normalize_text(str(item.get(field) or "")) for field in ("source_name", "title", "summary")):
            continue
        current_name = current_name_map.get(item["source_id"], "").strip()
        display_source_name = current_name or item.get("source_name") or ""
        display_time_raw = item.get("published_at") or item.get("published") or item.get("created_at") or ""
        sort_time = to_sortable_time(display_time_raw)
        source_site_url = current_site_map.get(item["source_id"], "").strip()
        link = normalize_entry_link(item.get("link") or "", source_site_url)
        if unread_only and (sort_time < ENTRIES_READ_CUTOFF or read_link_key(link) in read_keys):
            continue
        rows.append(
            {
                **item,
                "link": link,
                "has_link": bool(link),
                "source_name": display_source_name,
                "display_time": format_datetime(display_time_raw),
                "sort_time": sort_time,
            }
        )
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
        "unread_only": unread_only,
        "read_keys_text": ",".join(sorted(read_keys)),
    }


def get_laterhub_view(query: dict[str, str]) -> dict[str, Any]:
    status = load_status()
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
        "status": status,
        "filter_finished": filter_finished,
        "page": safe_page,
        "page_size": LATERHUB_PAGE_SIZE,
        "total_pages": total_pages,
    }


def get_settings_view(query: dict[str, str]) -> dict[str, Any]:
    status = load_status()
    status["success_sources_text"] = format_success_sources_text(status)
    status["last_run_inserted_entries"] = int(status.get("last_inserted_entries") or 0)
    status["run_log_text"] = read_log_tail(WEB_LOG_PATH)
    summary = get_laterhub_summary()
    laterhub_sources = get_laterhub_source_stats()
    stats = {item["source_id"]: item for item in list_source_stats()}
    keyword = normalize_text(query.get("source_q", ""))
    source_filter = query.get("source_filter", "")
    sort = query.get("sort", "name")
    direction = query.get("dir", "asc")
    wechat_auth = get_wechat_auth_status()
    wechat_login_url = f"/wechat-login?next={quote('/?view=settings', safe='')}"
    auth_assets = []
    for descriptor in list_auth_statuses():
        row = {
            "auth_key": descriptor.auth_key,
            "display_name": descriptor.display_name,
            "platform": descriptor.platform,
            "auth_mode": descriptor.auth_mode,
            "storage_ref": descriptor.storage_ref,
            "renew_strategy": descriptor.renew_strategy,
            "description": descriptor.description,
            "status_text": descriptor.status_text,
            "status_level": descriptor.status_level,
            "hint": descriptor.hint,
            "action_url": wechat_login_url if descriptor.auth_key == "wechat_mp_main" else "",
            "action_label": "续期/登录" if descriptor.auth_key == "wechat_mp_main" else "查看说明",
        }
        auth_assets.append(row)

    rows = []
    for item in normalize_sources():
        stat = stats.get(item["id"], {})
        source_health = source_runtime_health(item["id"])
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
            "invalid_days": invalid_days,
            "is_failed": bool(invalid_days),
            "invalid_sort": int(invalid_days) if invalid_days else -1,
            "enabled_sort": 1 if item.get("enabled") else 0,
            "enabled_text": "生效" if item.get("enabled") else "停用",
            "auth_asset_name": next((asset["display_name"] for asset in auth_assets if asset["auth_key"] == item.get("auth_key")), ""),
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
        "auth_assets": auth_assets,
        "q": query.get("source_q", ""),
        "source_filter": source_filter,
        "sort": sort,
        "dir": direction,
        "settings_raw": load_settings(),
        "wechat_auth": wechat_auth,
        "wechat_login_url": wechat_login_url,
    }

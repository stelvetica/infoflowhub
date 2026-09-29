from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from functools import cmp_to_key
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from apps.subscriptions.config import load_settings, load_sources, save_sources
from apps.subscriptions.rss_config import BSK_FAILURE_MARKER
from apps.subscriptions.rss_db import (
    delete_entries_by_source,
    delete_source_state,
    get_connection,
    list_read_entry_keys,
    list_source_stats,
    mark_entry_read,
    migrate_legacy_source_ids,
    rename_source,
    sanitize_db_text,
)
from apps.subscriptions.source_ids import canonicalize_source_id, legacy_source_ids, merge_source_health_rows
from connectors._shared.common import parse_published_datetime, resolve_web_target
from connectors.auth import list_auth_statuses, resolve_auth
from infra.text_normalizer import normalize_text_lines, normalize_utf8_obj, normalize_utf8_text
from infra.utf8_json import dump_json_utf8, load_json_utf8
from web.services.utils import (
    build_source_id,
    compare_value,
    sort_rows_with_tiebreak,
    take_after_cursor,
    format_date,
    format_datetime,
    join_tags,
    normalize_text,
    provider_label,
    read_entry_key,
    read_link_key,
    source_channel_label,
    split_read_keys,
    split_tags,
    to_sortable_time,
)

BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"
HEALTH_PATH = RUNTIME_DIR / "health" / "subscriptions_source_health.json"
STATUS_PATH = RUNTIME_DIR / "health" / "subscriptions_status.json"
LATERHUB_DB_PATH = BASE_DIR / "data" / "laterhub.sqlite3"
WEB_LOG_PATH = RUNTIME_DIR / "web.log"
LATERHUB_LOG_PATH = RUNTIME_DIR / "logs" / "laterhub.log"
# 自动化流程状态（AutoRunner 写、新闻面板按流程维度读），路径定义在数据层保证 SSOT
AUTOMATION_RUNTIME_PATH = RUNTIME_DIR / "health" / "automation_runtime.json"

DELETED_SITE_URLS = {"https://www.huxiu.com/member/2321131.html"}
# 每批条数要盖过常见窗口的可见行数:否则首屏填不满区格,续载会立刻补一批,
# 用户会看到「先少后多」的跳动。取 30(订阅行约 33px、稍后处理行约 60px,足够高)。
ENTRIES_BATCH_SIZE = 30
REPORTS_DIR = BASE_DIR / "data" / "reports"
# 「新闻」面板的数据源白名单：流程产物都计入（含流程中间产物 今日新闻.md），其余 json 一律不计入
REPORT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("新闻影响分析_*.md", "新闻影响分析"),
    ("公告清单_*.md", "公告清单"),
    ("公告清单_*.png", "公告图片"),
    ("今日新闻.md", "今日热点"),
    ("video_digest_*.md", "视频简报"),
)
# 订阅源 → 本地正文目录标识（路由 /markdown/{kind}/{name} 的 kind）
MARKDOWN_KINDS: dict[str, str] = {
    "alphapai": "alphapai",
    "jiuyangongshe_jiucaituanzi": "jiuyangongshe",
    "jiuyangongshe_jiujiubashiyi": "jiuyangongshe",
}
LATERHUB_BATCH_SIZE = 30  # 同 ENTRIES_BATCH_SIZE:首批必须盖过可见行数,避免续载紧接着补批造成跳动
ENTRIES_READ_CUTOFF = datetime(2026, 5, 1).timestamp()
# 资讯类来源（名字前缀即列表「来源」列里显示的分类标签）条目量大、属汇总型资讯，默认按已读处理：
# 不进未读列表，标题也按已读样式渲染。来源改名去掉前缀即恢复未读，故不另设字段。
DEFAULT_READ_SOURCE_PREFIXES = ("资讯",)
ABSOLUTE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
BILIBILI_DYNAMIC_PATH_RE = re.compile(r"^/(\d+)$")


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return load_json_utf8(path)
    except Exception:
        return fallback


def write_json(path: Path, value: Any) -> None:
    dump_json_utf8(path, value)


def read_log_tail(
    path: Path,
    max_lines: int = 80,
    last_run_only: bool = False,
    run_start_marker: str = "[1/6] 初始化 laterhub",
) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    if last_run_only:
        # ponytail: 仅取最近一次运行；以固定起始行切片，省去结构化日志头
        start = 0
        for idx in range(len(lines) - 1, -1, -1):
            if run_start_marker in lines[idx]:
                start = idx
                break
        # 新的在上：倒序输出该次运行
        picked = [lines[idx] for idx in range(len(lines) - 1, start - 1, -1)]
    else:
        picked = lines[-max_lines:]
    tail = [normalize_utf8_text(line) for line in picked if line.strip()]
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
    health = normalize_utf8_obj(read_json(HEALTH_PATH, {"sources": {}}))
    source_rows = health.get("sources", {})
    if not isinstance(source_rows, dict):
        return {"sources": {}}
    merged_sources = merge_source_health_rows(source_rows.items())
    normalized = {"sources": merged_sources}
    if merged_sources != source_rows:
        write_json(HEALTH_PATH, normalized)
    return normalized


def default_auth_key(channel: str, site_url: str, feed_url: str = "") -> str:
    normalized_site = normalize_text(site_url)
    normalized_feed = normalize_text(feed_url)
    combined = f"{normalized_site} {normalized_feed}"
    if "bilibili.com" in combined:
        return "bilibili_main"
    if "x.com/" in combined:
        return "x_profile2"
    if channel == "douyin":
        return "douyin_shared"
    if channel == "jiuyangongshe":
        return "jiuyangongshe_main"
    return ""


def auth_requirement_meta(auth_key: str) -> dict[str, str] | None:
    for descriptor in list_auth_statuses():
        if descriptor.auth_key != auth_key:
            continue
        requirement_map = {
            "douyin_shared": "依赖抖音共享登录态",
            "bilibili_main": "依赖 B站主账号登录态",
            "x_profile2": "依赖 X 平台共享登录态",
        }
        return {
            "requirement": requirement_map.get(auth_key, descriptor.display_name),
            "hint": normalize_utf8_text(descriptor.hint),
            "status_text": normalize_utf8_text(descriptor.status_text),
            "status_level": descriptor.status_level,
        }
    return None


def infer_source_meta(feed_url: str, site_url: str) -> tuple[str, str, str]:
    feed = normalize_text(feed_url)
    # 是否走浏览器抓取以 connector 侧的站点识别为准（唯一真源）；纯 RSS 源返回 None。
    # 原先这里另存一份硬编码域名白名单，两处不同步会把新站点误判成 native，
    # 保存后该源掉进 RSS 路径抓不到内容（Alpha派社媒即因此被降级）。
    if resolve_web_target({"feed_url": feed_url, "site_url": site_url}):
        return ("web", "web", "web")
    if "rsshub" in feed:
        return ("rsshub", "rsshub-self-hosted", "rsshub")
    return ("native", "direct", "native")


def infer_channel(feed_url: str, site_url: str) -> str:
    target = resolve_web_target({"feed_url": feed_url, "site_url": site_url})
    if target:
        return target.site
    combined = f"{normalize_text(feed_url)} {normalize_text(site_url)}"
    if "youtube.com" in combined or "youtu.be" in combined:
        return "youtube"
    if "rsshub" in combined:
        return "rsshub"
    return "rss"


def canonicalize_source(item: dict[str, Any]) -> dict[str, Any] | None:
    source_id = str(item.get("id") or "").strip()
    name = normalize_utf8_text(item.get("name"))
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
        "group": normalize_utf8_text(item.get("group")),
        "feed_url": feed_url,
        "site_url": site_url,
        "provider": provider,
        "fetch_via": fetch_via,
        "kind": kind,
        "enabled": bool(item.get("enabled", True)),
        "note": normalize_utf8_text(item.get("note")),
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


def normalize_sources(*, include_auth: bool = True) -> list[dict[str, Any]]:
    # 预算一次 auth 状态，避免每个源重复调 list_auth_statuses（N×M 重复验证）。
    # include_auth=False 跳过 auth 校验（慢，外部/Chrome 依赖），供主界面等不展示
    # 登录态的路径避免首载阻塞。
    descriptors = {d.auth_key: d for d in list_auth_statuses()} if include_auth else {}
    normalized: list[dict[str, Any]] = []
    for item in load_source_catalog():
        source = dict(item)
        descriptor = descriptors.get(source.get("auth_key", ""))
        if descriptor:
            source["login_requirement"] = descriptor.display_name
            source["login_hint"] = normalize_utf8_text(descriptor.hint)
            source["auth_status_text"] = normalize_utf8_text(descriptor.status_text)
            source["auth_status_level"] = descriptor.status_level
        else:
            source["login_requirement"] = ""
            source["login_hint"] = ""
            source["auth_status_text"] = ""
            source["auth_status_level"] = ""
        normalized.append(source)
    return normalized


def source_runtime_health(source_id: str) -> dict[str, str]:
    source_health = load_health().get("sources", {}).get(canonicalize_source_id(source_id), {})
    return {
        "last_checked_at": str(source_health.get("last_checked_at") or ""),
        "last_success_at": str(source_health.get("last_success_at") or ""),
        "last_failed_at": str(source_health.get("last_failed_at") or ""),
        "last_error": normalize_text_lines(source_health.get("last_error") or ""),
    }


def copy_retry_available(last_error: str) -> bool:
    """该源是否有 bsk 失败记录：只有这时才在设置页露出「用 copy 重试」按钮。"""
    return BSK_FAILURE_MARKER in (last_error or "")


def save_source(payload: dict[str, str]) -> None:
    raw_sources = [canonicalize_source(item) for item in load_sources()]
    raw_sources = [item for item in raw_sources if item]
    sources = load_source_catalog()
    existing = next((item for item in sources if item["id"] == payload.get("source_id", "").strip()), None)
    previous_name = str(existing.get("name") or "").strip() if existing else ""
    clean_feed_url = sanitize_db_text(payload["feed_url"]).strip()
    clean_site_url = sanitize_db_text(payload.get("site_url", "")).strip()
    clean_name = normalize_utf8_text(sanitize_db_text(payload["name"]).strip())
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
    clean_id = canonicalize_source_id(sanitize_db_text(source_id).strip())
    if not clean_id:
        return
    all_target_ids = {clean_id, *legacy_source_ids(clean_id)}
    save_sources([item for item in load_sources() if str(item.get("id") or "").strip() not in all_target_ids])
    delete_entries_by_source(clean_id)
    delete_source_state(clean_id)
    health = load_health()
    for target_id in all_target_ids:
        if health.get("sources", {}).get(target_id):
            del health["sources"][target_id]
    if health.get("sources") is not None:
        write_json(HEALTH_PATH, health)


def _load_entries(limit: int = 500) -> list[dict[str, Any]]:
    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            """
            SELECT source_id, source_name, title, link, published, published_at, summary, markdown_path, created_at
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
                SELECT id, url, title, tags, created_at, updated_at, is_finished, is_opened, opened_at, source
                FROM links
                ORDER BY id DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, url, title, tags, created_at, updated_at, is_finished, is_opened, opened_at, source
                FROM links
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _parse_laterhub_created_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        parsed = parse_published_datetime(text)
        if parsed:
            return parsed
    return None


def _laterhub_batch_key(value: str) -> str:
    created_at = _parse_laterhub_created_at(value)
    if created_at:
        return created_at.astimezone().strftime("%Y-%m-%d %H:%M")
    return str(value or "").strip()[:16]


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
        return {"total_count": 0, "unfinished_count": 0, "finished_count": 0, "opened_count": 0}
    conn = _laterhub_conn()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total_count,
                   SUM(CASE WHEN is_finished = 1 THEN 1 ELSE 0 END) AS finished_count,
                   SUM(CASE WHEN is_opened = 1 THEN 1 ELSE 0 END) AS opened_count,
                   SUM(CASE WHEN is_finished = 0 AND COALESCE(is_opened, 0) = 0 THEN 1 ELSE 0 END) AS unfinished_count
            FROM links
            """
        ).fetchone()
    finally:
        conn.close()
    return {
        "total_count": int(row["total_count"] or 0),
        "unfinished_count": int(row["unfinished_count"] or 0),
        "finished_count": int(row["finished_count"] or 0),
        "opened_count": int(row["opened_count"] or 0),
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
                   SUM(CASE WHEN is_finished = 0 AND COALESCE(is_opened, 0) = 0 THEN 1 ELSE 0 END) AS unfinished_count
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


def mark_laterhub_opened(link_id: int, opened: bool = True) -> None:
    if not LATERHUB_DB_PATH.exists():
        return
    conn = _laterhub_conn()
    try:
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            UPDATE links
            SET is_opened = ?,
                opened_at = CASE
                    WHEN ? = 1 THEN COALESCE(opened_at, ?)
                    ELSE NULL
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (1 if opened else 0, 1 if opened else 0, now_text, now_text, link_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_laterhub_finished_bulk(link_ids: list[int], finished: bool) -> int:
    if not LATERHUB_DB_PATH.exists() or not link_ids:
        return 0
    normalized_ids = sorted({int(link_id) for link_id in link_ids})
    placeholders = ", ".join("?" for _ in normalized_ids)
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _laterhub_conn()
    try:
        cursor = conn.execute(
            f"""
            UPDATE links
            SET is_finished = ?, finished_at = ?, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (1 if finished else 0, now_text if finished else None, now_text, *normalized_ids),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _build_laterhub_rows() -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    all_rows: list[dict[str, Any]] = []
    latest_batch_key = ""
    latest_created_at: datetime | None = None
    today = datetime.now().date()
    scope_counts = {"all": 0, "current_batch": 0, "today": 0}
    for item in _load_laterhub_items():
        raw_tags_text = normalize_utf8_text(item.get("tags"))
        tag_list = split_tags(raw_tags_text)
        tags_text = join_tags(tag_list)
        created_at_text = str(item.get("created_at") or "")
        created_at = _parse_laterhub_created_at(created_at_text)
        batch_key = _laterhub_batch_key(created_at_text)
        local_created_date = created_at.astimezone().date() if created_at and created_at.tzinfo else (created_at.date() if created_at else None)
        row = {
            **item,
            "title": normalize_utf8_text(item.get("title")),
            "display_time": format_date(created_at_text),
            "sort_time": to_sortable_time(created_at_text),
            "raw_tags_text": raw_tags_text,
            "tags_text": tags_text,
            "tag_list": tag_list,
            "tag_keys": {normalize_text(tag) for tag in tag_list},
            "created_at_dt": created_at,
            "batch_key": batch_key,
            "is_today": bool(local_created_date == today),
            "is_opened": bool(item.get("is_opened")),
        }
        all_rows.append(row)
        scope_counts["all"] += 1
        if row["is_today"]:
            scope_counts["today"] += 1
        if created_at and (latest_created_at is None or created_at > latest_created_at):
            latest_created_at = created_at
            latest_batch_key = batch_key
    if latest_batch_key:
        scope_counts["current_batch"] = sum(1 for item in all_rows if item["batch_key"] == latest_batch_key)
    return all_rows, latest_batch_key, scope_counts


def _filter_laterhub_rows(query: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, int], str]:
    keyword = normalize_text(query.get("laterhub_q", ""))
    filter_finished = query.get("laterhub_filter_finished", "0")
    selected_tags = split_tags(query.get("laterhub_filter_tag", ""))
    selected_tag = selected_tags[0] if selected_tags else ""
    selected_key = normalize_text(selected_tag)
    filter_scope = query.get("laterhub_filter_scope", "")
    all_rows, latest_batch_key, scope_counts = _build_laterhub_rows()
    rows: list[dict[str, Any]] = []
    for item in all_rows:
        if keyword and keyword not in normalize_text(item["title"]) and keyword not in normalize_text(item["tags_text"]):
            continue
        if filter_finished == "1" and not item["is_finished"]:
            continue
        if filter_finished == "opened" and not item["is_opened"]:
            continue
        if filter_finished == "0" and item["is_finished"]:
            continue
        if filter_finished == "0" and item["is_opened"]:
            continue
        if selected_key and selected_key not in item["tag_keys"]:
            continue
        if filter_scope == "current_batch" and item["batch_key"] != latest_batch_key:
            continue
        if filter_scope == "today" and not item["is_today"]:
            continue
        rows.append(item)
    return rows, all_rows, selected_tag, scope_counts, filter_scope


def build_laterhub_state_params(query: Mapping[str, Any]) -> dict[str, str]:
    return {
        "laterhub_q": str(query.get("laterhub_q", "") or ""),
        "laterhub_sort": str(query.get("laterhub_sort") or "sort_time"),
        "laterhub_dir": str(query.get("laterhub_dir") or "desc"),
        "laterhub_filter_finished": str(query.get("laterhub_filter_finished", "0") or "0"),
        "laterhub_filter_tag": str(query.get("laterhub_filter_tag", "") or ""),
        "laterhub_filter_scope": str(query.get("laterhub_filter_scope", "") or ""),
    }


def build_laterhub_query_string(query: Mapping[str, Any]) -> str:
    return urlencode({key: value for key, value in build_laterhub_state_params(query).items() if value != ""})


def build_entries_state_params(query: Mapping[str, Any]) -> dict[str, str]:
    unread_only = query.get("entries_unread_only")
    return {
        "entries_q": str(query.get("entries_q", "") or ""),
        "entries_sort": str(query.get("entries_sort") or "sort_time"),
        "entries_dir": str(query.get("entries_dir") or "desc"),
        "entries_unread_only": "1" if unread_only is None else str(unread_only),
        "entries_read_keys": str(query.get("entries_read_keys", "") or ""),
    }


def build_news_state_params(query: Mapping[str, Any]) -> dict[str, str]:
    return {
        "news_q": str(query.get("news_q", "") or ""),
        "news_sort": str(query.get("news_sort") or "sort_time"),
        "news_dir": str(query.get("news_dir") or "desc"),
        # 新闻面板的未读切换控件已移除,恒按「显示全部」筛:这里忽略 query 里的值,
        # 是为了让浏览器里残留的旧 cookie(news_unread_only=1)不能把产物静默过滤掉。
        # 订阅面板不受影响,仍缺省只看未读(build_entries_state_params 里为 "1")。
        "news_unread_only": "0",
        "news_read_keys": str(query.get("news_read_keys", "") or ""),
        # 范围默认与 get_news_view 一致：flow（本次流程产物）；显式 all 才扫全量白名单
        "news_scope": "all" if query.get("news_scope") == "all" else "flow",
    }


def _build_entry_row(item: dict[str, Any], current_name_map: dict[str, str], current_site_map: dict[str, str]) -> dict[str, Any]:
    """把一条原始条目构建为可渲染行(链接规范化/read_key/本地正文改写/时间)。"""
    current_name = current_name_map.get(item["source_id"], "").strip()
    display_source_name = current_name or normalize_utf8_text(item.get("source_name")) or ""
    display_time_raw = item.get("published_at") or item.get("published") or item.get("created_at") or ""
    sort_time = to_sortable_time(display_time_raw)
    source_site_url = current_site_map.get(item["source_id"], "").strip()
    link = normalize_entry_link(item.get("link") or "", source_site_url)
    # 顺序硬约束：read_key 必须先按「改写前的原始 link」算（历史已读态 = sha1(原始 link)），
    # 之后才把展示 link 改写成仓内可读路由，否则已读态全部失效。
    entry_read_key = read_entry_key(item["source_id"], link, str(item.get("title") or ""))
    markdown_path = str(item.get("markdown_path") or "").strip()
    display_link = markdown_url(item["source_id"], markdown_path) or link
    return {
        **item,
        "title": normalize_utf8_text(item.get("title")),
        "summary": normalize_utf8_text(item.get("summary")),
        "link": display_link,
        "markdown_path": markdown_path,
        "read_key": entry_read_key,
        "has_link": bool(display_link),
        "source_name": display_source_name,
        "display_time": format_datetime(display_time_raw),
        "sort_time": sort_time,
        "default_read": display_source_name.startswith(DEFAULT_READ_SOURCE_PREFIXES),
    }


def _iter_entry_rows(
    *,
    source_ids_filter: set[str] | None,
    excluded_prefixes: tuple[str, ...] = (),
    keyword: str,
    unread_only: bool,
    effective_read_keys: set[str],
    load_limit: int,
) -> list[dict[str, Any]]:
    """按来源过滤/关键字/未读态过滤并构建全部可见行,不排序。"""
    current_sources = normalize_sources(include_auth=False)
    enabled_ids = {item["id"] for item in current_sources if item.get("enabled")}
    current_name_map = {item["id"]: str(item.get("name") or "").strip() for item in current_sources}
    current_site_map = {item["id"]: str(item.get("site_url") or "").strip() for item in current_sources}
    excluded_ids = {
        source_id
        for source_id, name in current_name_map.items()
        if name and any(name.startswith(prefix) for prefix in excluded_prefixes)
    }
    rows = []
    for item in _load_entries(load_limit):
        if item["source_id"] not in enabled_ids:
            continue
        if item["source_id"] in excluded_ids:
            continue
        if source_ids_filter is not None and item["source_id"] not in source_ids_filter:
            continue
        if keyword and not any(keyword in normalize_text(str(item.get(field) or "")) for field in ("source_name", "title", "summary")):
            continue
        row = _build_entry_row(item, current_name_map, current_site_map)
        if unread_only and (row["default_read"] or row["sort_time"] < ENTRIES_READ_CUTOFF or row["read_key"] in effective_read_keys):
            continue
        rows.append(row)
    return rows


ENTRIES_LOAD_LIMIT = 500


def get_entries_view(query: dict[str, str]) -> dict[str, Any]:
    # 条目列表不展示登录态，跳过 auth 校验避免主界面首载阻塞
    keyword = normalize_text(query.get("entries_q", ""))
    unread_only = query.get("entries_unread_only", "0") == "1"
    read_keys = set(split_read_keys(query.get("entries_read_keys", "")))
    persisted_read_keys = list_read_entry_keys()
    effective_read_keys = read_keys | persisted_read_keys
    sort = query.get("entries_sort") or "sort_time"
    direction = query.get("entries_dir") or "desc"
    cursor = query.get("entries_after", "")
    rows = _iter_entry_rows(
        source_ids_filter=None,
        keyword=keyword,
        unread_only=unread_only,
        effective_read_keys=effective_read_keys,
        load_limit=ENTRIES_LOAD_LIMIT,
    )
    sort_rows_with_tiebreak(rows, sort, direction, "read_key")
    batch, next_cursor = take_after_cursor(rows, cursor, sort, direction, "read_key", ENTRIES_BATCH_SIZE)
    return {
        "rows": batch,
        "sort": sort,
        "dir": direction,
        "q": query.get("entries_q", ""),
        "next_cursor": next_cursor,
        "filtered_total": len(rows),
        "total": _count_entries(),
        "unread_only": unread_only,
        "read_keys_text": ",".join(sorted(effective_read_keys)),
    }


NEWS_BATCH_SIZE = 5


def markdown_url(source_id: str, markdown_path: str) -> str:
    """把条目的本地 markdown 路径映射为可读路由；无 kind 映射或路径为空时返回空串。"""
    kind = MARKDOWN_KINDS.get(str(source_id or "").strip(), "")
    name = Path(str(markdown_path or "").strip()).name
    if not kind or not name:
        return ""
    return f"/markdown/{kind}/{quote(name, safe='')}"


def get_flow_state() -> dict[str, Any]:
    """读取最近一次自动化流程状态（flow 对象）；文件缺失或无该字段时返回空 dict。"""
    state = read_json(AUTOMATION_RUNTIME_PATH, {})
    flow = state.get("flow") if isinstance(state, dict) else None
    return flow if isinstance(flow, dict) else {}


def _build_report_row(path: Path, kind: str) -> dict[str, Any]:
    """单文件 → 完整产物行；白名单扫描与流程产物补全共用（SSOT）。"""
    matched = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
    if matched:
        date_text = matched.group(0)
        display_time = date_text.replace("-", "/")
        sort_time = to_sortable_time(date_text)
    else:
        # 文件名没日期时用文件 mtime 兜底，保证排序稳定可读
        fallback = datetime.fromtimestamp(path.stat().st_mtime)
        display_time = fallback.strftime("%Y/%m/%d")
        sort_time = fallback.timestamp()
    return {
        "name": path.name,
        "kind": kind,
        "is_image": path.suffix.lower() == ".png",
        "title": path.stem,
        "link": f"/reports/view/{quote(path.name, safe='')}",
        "has_link": True,
        "read_key": f"report:{path.name}",
        "display_time": display_time,
        "sort_time": sort_time,
    }


def _iter_report_rows() -> list[dict[str, Any]]:
    """扫描产物目录（白名单 glob）构建「生成产物」行；目录不存在时返回空列表。"""
    rows: list[dict[str, Any]] = []
    for pattern, kind in REPORT_PATTERNS:
        for path in sorted(REPORTS_DIR.glob(pattern)):
            if not path.is_file():
                continue
            rows.append(_build_report_row(path, kind))
    return rows


def _iter_flow_report_rows(flow: dict[str, Any]) -> list[dict[str, Any]]:
    """按最近一次流程的 products 清单补全为完整行；清单缺项或文件已被清理的条目跳过。"""
    products = flow.get("products")
    if not isinstance(products, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in products:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        path = REPORTS_DIR / name
        if not name or not path.is_file():
            continue
        rows.append(_build_report_row(path, str(item.get("kind") or "")))
    return rows


def get_news_view(query: dict[str, str]) -> dict[str, Any]:
    """新闻卡片：展示 data/reports 下的生成产物，游标式续载（每批 NEWS_BATCH_SIZE 条）。

    news_scope=flow（默认）只看最近一次流程新增的产物，=all 扫全部白名单文件。
    """
    keyword = normalize_text(query.get("news_q", ""))
    unread_only = query.get("news_unread_only", "0") == "1"
    read_keys = set(split_read_keys(query.get("news_read_keys", "")))
    persisted_read_keys = list_read_entry_keys()
    effective_read_keys = read_keys | persisted_read_keys
    sort = query.get("news_sort") or "sort_time"
    direction = query.get("news_dir") or "desc"
    scope = "all" if query.get("news_scope") == "all" else "flow"
    source_rows = _iter_report_rows() if scope == "all" else _iter_flow_report_rows(get_flow_state())
    cursor = query.get("news_after", "")
    rows: list[dict[str, Any]] = []
    for row in source_rows:
        if keyword and keyword not in normalize_text(row["title"]):
            continue
        # 产物是新文件，不套用 ENTRIES_READ_CUTOFF（历史条目才需要按时间兜底判已读）
        if unread_only and row["read_key"] in effective_read_keys:
            continue
        rows.append(row)
    sort_rows_with_tiebreak(rows, sort, direction, "read_key")
    batch, next_cursor = take_after_cursor(rows, cursor, sort, direction, "read_key", NEWS_BATCH_SIZE)
    return {
        "rows": batch,
        "sort": sort,
        "dir": direction,
        "q": query.get("news_q", ""),
        "next_cursor": next_cursor,
        "filtered_total": len(rows),
        "unread_only": unread_only,
        "read_keys_text": ",".join(sorted(effective_read_keys)),
        "scope": scope,
    }


def mark_entry_read_state(read_key: str) -> bool:
    return mark_entry_read(read_key)


def get_laterhub_view(query: dict[str, str]) -> dict[str, Any]:
    status = load_status()
    sort = query.get("laterhub_sort") or "sort_time"
    direction = query.get("laterhub_dir") or "desc"
    cursor = query.get("laterhub_after", "")
    rows, all_rows, selected_tag, scope_counts, filter_scope = _filter_laterhub_rows(query)
    filter_finished = query.get("laterhub_filter_finished", "0")
    sort_rows_with_tiebreak(rows, sort, direction, "id")
    all_tags_map: dict[str, str] = {}
    for item in all_rows:
        for tag in item["tag_list"]:
            key = normalize_text(tag)
            if key not in all_tags_map:
                all_tags_map[key] = tag
    all_tags = [all_tags_map[key] for key in sorted(all_tags_map.keys())]
    batch, next_cursor = take_after_cursor(rows, cursor, sort, direction, "id", LATERHUB_BATCH_SIZE)
    actionable_ids = [int(item["id"]) for item in rows if not item["is_finished"]]
    state_params = build_laterhub_state_params(
        {
            **query,
            "laterhub_filter_tag": selected_tag,
            "laterhub_filter_scope": filter_scope,
        }
    )
    return {
        "rows": batch,
        "total": count_laterhub_items(),
        "filtered_total": len(rows),
        "bulk_finishable_count": len(actionable_ids),
        "opened_count": sum(1 for item in all_rows if item["is_opened"]),
        "all_tags": all_tags,
        "selected_tags": [selected_tag] if selected_tag else [],
        "selected_tags_text": selected_tag,
        "filter_scope": filter_scope,
        "scope_counts": scope_counts,
        "sort": sort,
        "dir": direction,
        "q": query.get("laterhub_q", ""),
        "status": status,
        "filter_finished": filter_finished,
        "next_cursor": next_cursor,
        "state_params": state_params,
        "state_query": build_laterhub_query_string(state_params),
    }


def get_laterhub_actionable_ids(query: dict[str, str]) -> list[int]:
    rows, _, _, _, _ = _filter_laterhub_rows(query)
    return [int(item["id"]) for item in rows if not item["is_finished"]]


def get_nav_settings() -> dict[str, Any]:
    """主界面(entries view)用的轻量设置切片。

    只取主界面需要的字段（运行状态的成功源数），避免每次加载主界面都跑
    get_settings_view 的全量源统计（慢）。
    """
    status = load_status()
    status["success_sources_text"] = format_success_sources_text(status)
    status["last_run_inserted_entries"] = int(status.get("last_inserted_entries") or 0)
    last_error = normalize_text_lines(status.get("last_error"))
    status["last_error"] = last_error
    error_timestamp = str(status.get("last_run_at") or status.get("current_run_started_at") or "")
    status["error_log_text"] = f"{error_timestamp} {last_error}".strip() if last_error else ""
    return {
        "status": status,
    }


def get_settings_view(query: dict[str, str]) -> dict[str, Any]:
    migrate_legacy_source_ids()
    status = load_status()
    status["success_sources_text"] = format_success_sources_text(status)
    status["last_run_inserted_entries"] = int(status.get("last_inserted_entries") or 0)
    last_error = normalize_text_lines(status.get("last_error"))
    status["last_error"] = last_error
    error_timestamp = str(status.get("last_run_at") or status.get("current_run_started_at") or "")
    status["error_log_text"] = f"{error_timestamp} {last_error}".strip() if last_error else ""
    laterhub_log_text = read_log_tail(LATERHUB_LOG_PATH, last_run_only=True)
    summary = get_laterhub_summary()
    laterhub_sources = get_laterhub_source_stats()
    stats = {item["source_id"]: item for item in list_source_stats()}
    keyword = normalize_text(query.get("source_q", ""))
    source_filter = query.get("source_filter", "")
    sort = query.get("sort", "name")
    direction = query.get("dir", "asc")
    auth_assets = []
    for descriptor in list_auth_statuses():
        expire_summary = ""
        expire_at_text = ""
        hint = normalize_utf8_text(descriptor.hint)
        renew_action_url = ""
        renew_action_label = ""
        is_expired = False
        is_expiring_soon = False
        nickname = normalize_utf8_text(descriptor.nickname)
        if descriptor.auth_key == "bilibili_main":
            renew_action_url = "/actions/bilibili-login/sync"
            renew_action_label = "同步登录态"
            is_expired = bool(descriptor.is_expired)
        row = {
            "auth_key": descriptor.auth_key,
            "display_name": normalize_utf8_text(descriptor.display_name),
            "platform": descriptor.platform,
            "auth_mode": descriptor.auth_mode,
            "login_method": normalize_utf8_text(getattr(descriptor, "login_method", "")),
            "renew_label": normalize_utf8_text(reg.renew_label) if (reg := resolve_auth(descriptor.auth_key)).renew_label else "",
            "storage_ref": descriptor.storage_ref,
            "renew_strategy": descriptor.renew_strategy,
            "description": normalize_utf8_text(descriptor.description),
            "status_text": normalize_utf8_text(descriptor.status_text),
            "status_level": descriptor.status_level,
            "hint": hint,
            "renew_action_url": renew_action_url,
            "renew_action_label": renew_action_label,
            "expire_summary": expire_summary,
            "expire_at_text": expire_at_text,
            "is_expired": is_expired,
            "is_expiring_soon": is_expiring_soon,
            "nickname": nickname,
        }
        auth_assets.append(row)

    rows = []
    for item in normalize_sources():
        stat = stats.get(item["id"], {})
        source_health = source_runtime_health(item["id"])
        failed_at = parse_published_datetime(source_health.get("last_failed_at", "") or "")
        success_at = parse_published_datetime(source_health.get("last_success_at", "") or "")
        last_error = normalize_text_lines(source_health.get("last_error") or "")
        invalid_days = ""
        if failed_at and (not success_at or failed_at > success_at):
            invalid_days = str(max((datetime.now() - failed_at).days, 0))
        row = {
            **item,
            "provider_label": provider_label(str(item.get("provider") or ""), str(item.get("fetch_via") or "")),
            "channel_label": source_channel_label(str(item.get("feed_url") or ""), str(item.get("site_url") or ""), str(item.get("provider") or "")),
            "entry_count": int(stat.get("entry_count") or 0),
            "last_error": last_error,
            "copy_retry_available": copy_retry_available(last_error),
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
        "laterhub_log_text": laterhub_log_text,
        "q": query.get("source_q", ""),
        "source_filter": source_filter,
        "sort": sort,
        "dir": direction,
        "settings_raw": load_settings(),
    }

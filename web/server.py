from __future__ import annotations

import html
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from apps.laterhub.config import DB_PATH as LATERHUB_DB_PATH
from apps.subscriptions.config import load_settings, load_sources, save_sources
from apps.subscriptions.storage import delete_entries_by_source, list_entries, list_source_stats, save_entries
from apps.subscriptions.rss_db import delete_source_state, list_source_enabled_state, set_source_enabled
from connectors.rss.fetch import fetch_many, resolve_feed_url


BASE_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = BASE_DIR / "runtime"
STATUS_PATH = RUNTIME_DIR / "health" / "subscriptions_status.json"
HEALTH_PATH = RUNTIME_DIR / "health" / "subscriptions_source_health.json"
HOST = "127.0.0.1"
PORT = 18421
SCHEDULE_HOURS = (6, 16)


def ensure_runtime() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def next_scheduled_run(now: datetime | None = None) -> datetime:
    current = now or datetime.now()
    for hour in SCHEDULE_HOURS:
        candidate = current.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > current:
            return candidate
    tomorrow = current + timedelta(days=1)
    return tomorrow.replace(hour=SCHEDULE_HOURS[0], minute=0, second=0, microsecond=0)


def latest_scheduled_run(now: datetime | None = None) -> datetime | None:
    current = now or datetime.now()
    for hour in reversed(SCHEDULE_HOURS):
        candidate = current.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate <= current:
            return candidate
    return None


def to_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for pattern in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def format_datetime(value: str) -> str:
    dt = to_datetime(value)
    if dt:
        return dt.strftime("%Y/%m/%d %H:%M")
    text = (value or "").strip()
    if not text:
        return ""
    return text[:16].replace("-", "/")


def sortable_datetime(value: str) -> datetime:
    dt = to_datetime(value)
    if not dt:
        return datetime.min
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def load_json(path: Path, default: dict) -> dict:
    ensure_runtime()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload: dict) -> None:
    ensure_runtime()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_status() -> dict:
    return load_json(
        STATUS_PATH,
        {
            "last_run_at": "",
            "last_success_at": "",
            "last_error": "",
            "last_total_sources": 0,
            "last_success_sources": 0,
            "last_inserted_entries": 0,
        },
    )


def save_status(status: dict) -> None:
    save_json(STATUS_PATH, status)


def load_health() -> dict:
    return load_json(HEALTH_PATH, {"sources": {}})


def save_health(health: dict) -> None:
    save_json(HEALTH_PATH, health)


def clean_health_error(value: str) -> str:
    text = " ".join((value or "").strip().split())
    return text[:320]


def provider_label(source: dict) -> str:
    provider = source.get("provider", "native")
    fetch_via = source.get("fetch_via", "")
    if provider == "rsshub":
        if fetch_via == "rsshub-public":
            return "RSSHub 公共"
        return "RSSHub"
    if provider == "web":
        return "网页直抓"
    return "原生 RSS"


def normalize_text(value: str) -> str:
    return (value or "").strip().lower()


def split_tag_values(value: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        return []
    normalized = text
    for sep in ("、", "，", ";", "；", "|"):
        normalized = normalized.replace(sep, ",")
    parts: list[str] = []
    seen: set[str] = set()
    for part in normalized.split(","):
        tag = part.strip()
        if not tag:
            continue
        key = normalize_text(tag)
        if key in seen:
            continue
        seen.add(key)
        parts.append(tag)
    return parts


def join_tag_values(tags: list[str]) -> str:
    return ",".join(tag.strip() for tag in tags if tag.strip())


def normalize_sources() -> list[dict]:
    sources = load_sources()
    enabled_state = list_source_enabled_state()
    changed = False
    seen_ids: set[str] = set()
    seen_feeds: set[str] = set()
    normalized: list[dict] = []
    web_feed_urls = {
        "https://rsshub.app/bilibili/user/dynamic/14089380",
        "https://rsshub.app/bilibili/user/dynamic/474921808",
        "https://rsshub.app/bilibili/user/dynamic/162183",
        "https://rsshub.app/bilibili/user/dynamic/1908067732",
        "https://rsshub.app/bilibili/user/dynamic/2117498259",
        "https://rsshub.app/bilibili/user/dynamic/472747194",
        "https://rsshub.app/bilibili/user/dynamic/316183842",
        "https://rsshub.app/bilibili/user/dynamic/1257954297",
        "https://rsshub.app/bilibili/user/dynamic/381870733",
        "https://rsshub.app/bilibili/user/video/3546909549529340",
        "https://rsshub.app/bilibili/user/dynamic/2233213",
        "https://rsshub.app/weibo/user/2014433131",
        "https://rsshub.app/weibo/user/7782629809",
        "https://rsshub.app/twitter/user/MacroMargin",
    }
    native_feed_urls = {
        "https://www.supertechfans.com/cn/index.xml",
        "https://www.youtube.com/feeds/videos.xml?channel_id=UC7eBKmeAz99qswOcm3VxOow",
        "https://www.youtube.com/feeds/videos.xml?channel_id=UC8gZZWIWmBuCb_gzC8DUrvw",
        "https://feeds.feedburner.com/ruanyifeng",
        "https://lumina.shawnxie.top/backend/api/reviews/rss.xml",
    }

    alias_rules = {
        "https://rsshub.app/bilibili/user/video/3546909549529340": {
            "name": "外资观点 小鹿投研日记 的 bilibili 动态",
            "site_url": "https://space.bilibili.com/3546909549529340",
        },
        "https://rsshub.app/bilibili/user/dynamic/14089380": {
            "name": "技术 算法 labuladong 的 bilibili 动态",
            "site_url": "https://space.bilibili.com/14089380/dynamic",
        },
        "https://rsshub.app/bilibili/user/dynamic/1908067732": {
            "name": "观点 路口大爷聊宏观 的 bilibili 动态",
            "site_url": "https://space.bilibili.com/1908067732/dynamic",
        },
        "https://rsshub.app/bilibili/user/dynamic/2117498259": {
            "name": "基金 硬核姬老板 的 bilibili 动态",
            "site_url": "https://space.bilibili.com/2117498259/dynamic",
        },
        "https://rsshub.app/bilibili/user/dynamic/472747194": {
            "name": "产业 科普 巫师财经 的 bilibili 动态",
            "site_url": "https://space.bilibili.com/472747194/dynamic",
        },
        "https://rsshub.app/bilibili/user/dynamic/1257954297": {
            "name": "观点 房产 铁锤观察室 的 bilibili 动态",
            "site_url": "https://space.bilibili.com/1257954297/dynamic",
        },
        "https://rsshub.app/bilibili/user/dynamic/381870733": {
            "name": "外资观点 小黄的投资笔记 的 bilibili 动态",
            "site_url": "https://space.bilibili.com/381870733/dynamic",
        },
        "https://rsshub.app/bilibili/user/dynamic/2233213": {
            "name": "时事 短评 长文视频 星话大白 的 bilibili 动态",
            "site_url": "https://space.bilibili.com/2233213/dynamic",
        },
        "https://lumina.shawnxie.top/backend/api/reviews/rss.xml": {
            "name": "技术 肖恩周刊",
            "site_url": "https://lumina.shawnxie.top/",
        },
    }
    deleted_site_urls = {
        "https://www.huxiu.com/member/2321131.html",
    }

    for item in sources:
        feed_url = (item.get("feed_url") or "").strip()
        site_url = (item.get("site_url") or "").strip()
        if site_url in deleted_site_urls:
            changed = True
            continue
        if not feed_url:
            changed = True
            continue

        source = dict(item)
        source["name"] = (source.get("name") or "").strip()
        source["site_url"] = site_url
        source["feed_url"] = feed_url
        source["enabled"] = bool(enabled_state.get(source.get("id"), source.get("enabled", True)))

        if feed_url in alias_rules:
            for key, value in alias_rules[feed_url].items():
                if source.get(key) != value:
                    source[key] = value
                    changed = True

        if "bilibili.com" in (feed_url + " " + site_url).lower() and not source["name"].endswith("bilibili 动态"):
            source["name"] = f"{source['name']} bilibili 动态"
            changed = True

        if feed_url in native_feed_urls:
            if source.get("provider") != "native":
                source["provider"] = "native"
                changed = True
            if source.get("fetch_via") != "direct":
                source["fetch_via"] = "direct"
                changed = True
            if source.get("kind") != "native":
                source["kind"] = "native"
                changed = True
        elif feed_url in web_feed_urls:
            if source.get("provider") != "web":
                source["provider"] = "web"
                changed = True
            if source.get("fetch_via") != "web":
                source["fetch_via"] = "web"
                changed = True
            if source.get("kind") != "web":
                source["kind"] = "web"
                changed = True

        kind = source.get("kind") or source.get("provider") or "native"
        if kind == "rsshub":
            if source.get("provider") != "rsshub":
                source["provider"] = "rsshub"
                changed = True
            if source.get("fetch_via") not in {"rsshub-self-hosted", "rsshub-public"}:
                source["fetch_via"] = "rsshub-self-hosted"
                changed = True
        elif kind == "web":
            if source.get("provider") != "web":
                source["provider"] = "web"
                changed = True
            if source.get("fetch_via") != "web":
                source["fetch_via"] = "web"
                changed = True
            source["kind"] = "web"
        else:
            if source.get("provider") != "native":
                source["provider"] = "native"
                changed = True
            if source.get("fetch_via") != "direct":
                source["fetch_via"] = "direct"
                changed = True
            source["kind"] = "native"

        source_id = (source.get("id") or "").strip()
        if not source_id:
            source_id = build_source_id(source["name"])
            source["id"] = source_id
            changed = True

        if source_id in seen_ids or feed_url in seen_feeds:
            changed = True
            continue

        seen_ids.add(source_id)
        seen_feeds.add(feed_url)
        normalized.append(source)

    if changed or len(normalized) != len(sources):
        save_sources(normalized)
    return normalized


def build_source_id(name: str) -> str:
    value = (name or "").strip().lower()
    for old, new in (
        (" ", "-"),
        ("/", "-"),
        ("\\", "-"),
        (":", "-"),
        ("，", "-"),
        (",", "-"),
        ("（", "-"),
        ("）", "-"),
        ("(", "-"),
        (")", "-"),
    ):
        value = value.replace(old, new)
    return "-".join(part for part in value.split("-") if part) or f"source-{int(time.time())}"


def list_laterhub_items(limit: int = 500) -> list[dict]:
    if not LATERHUB_DB_PATH.exists():
        return []
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
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


def list_laterhub_summary() -> dict:
    if not LATERHUB_DB_PATH.exists():
        return {"total_count": 0, "unfinished_count": 0, "finished_count": 0}
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    conn.row_factory = sqlite3.Row
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
    return dict(row) if row else {"total_count": 0, "unfinished_count": 0, "finished_count": 0}


def laterhub_source_meta(source: str) -> dict[str, str]:
    mapping = {
        "bilibili_watchlater": {
            "label": "B站稍后看",
            "purpose": "收藏/稍后处理",
            "fetch_mode": "私有API登录态",
        },
        "douyin_favorite": {
            "label": "抖音收藏",
            "purpose": "收藏/稍后处理",
            "fetch_mode": "私有网页登录态",
        },
        "manual_verify": {
            "label": "人工补录",
            "purpose": "资料核实/待整理",
            "fetch_mode": "人工录入",
        },
    }
    return mapping.get(
        source,
        {
            "label": source,
            "purpose": "待分类",
            "fetch_mode": "待识别",
        },
    )


def list_laterhub_source_stats() -> list[dict]:
    if not LATERHUB_DB_PATH.exists():
        return []
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    conn.row_factory = sqlite3.Row
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
    result = []
    for row in rows:
        item = dict(row)
        meta = laterhub_source_meta(item["source"])
        item["label"] = meta["label"]
        item["purpose"] = meta["purpose"]
        item["fetch_mode"] = meta["fetch_mode"]
        result.append(item)
    return result


def mark_laterhub_finished(link_id: int, finished: bool) -> None:
    if not LATERHUB_DB_PATH.exists():
        return
    conn = sqlite3.connect(LATERHUB_DB_PATH)
    try:
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            UPDATE links
            SET is_finished = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (1 if finished else 0, now_iso if finished else None, now_iso, link_id),
        )
        conn.commit()
    finally:
        conn.close()


def create_source_from_form(form: dict[str, list[str]]) -> dict:
    source_id = form.get("source_id", [""])[0].strip()
    name = form.get("name", [""])[0].strip()
    feed_url = form.get("feed_url", [""])[0].strip()
    site_url = form.get("site_url", [""])[0].strip()
    existing = next((item for item in normalize_sources() if item["id"] == source_id), None)
    provider = existing.get("provider", "native") if existing else ("rsshub" if "rsshub" in feed_url else "native")
    fetch_via = existing.get("fetch_via", "direct") if existing else ("rsshub-self-hosted" if provider == "rsshub" else "direct")
    kind = "rsshub" if provider == "rsshub" else ("web" if provider == "web" else "native")
    return {
        "id": source_id or build_source_id(name),
        "name": name,
        "group": existing.get("group", "手动新增") if existing else "手动新增",
        "feed_url": feed_url,
        "site_url": site_url,
        "provider": provider,
        "fetch_via": fetch_via,
        "kind": kind,
        "enabled": True if existing is None else bool(existing.get("enabled", True)),
        "note": existing.get("note", "") if existing else "",
    }


def save_source_from_form(form: dict[str, list[str]]) -> None:
    target = create_source_from_form(form)
    if not target["name"] or not target["feed_url"]:
        return
    sources = normalize_sources()
    saved = []
    replaced = False
    for item in sources:
        if item["id"] == target["id"]:
            saved.append(target)
            replaced = True
        else:
            saved.append(item)
    if not replaced:
        saved.append(target)
    save_sources(saved)
    set_source_enabled(target["id"], bool(target.get("enabled", True)))


def update_source_enabled(source_id: str, enabled: bool) -> None:
    if not source_id:
        return
    sources = normalize_sources()
    saved = []
    changed = False
    for item in sources:
        current = dict(item)
        if item["id"] == source_id:
            current["enabled"] = enabled
            changed = True
        saved.append(current)
    if changed:
        save_sources(saved)
        set_source_enabled(source_id, enabled)


def delete_source(source_id: str) -> None:
    sources = [item for item in normalize_sources() if item["id"] != source_id]
    save_sources(sources)
    delete_entries_by_source(source_id)
    delete_source_state(source_id)
    health = load_health()
    source_health = health.setdefault("sources", {})
    if source_id in source_health:
        del source_health[source_id]
        save_health(health)


def update_source_health(result) -> None:
    health = load_health()
    source_health = health.setdefault("sources", {})
    current = source_health.get(result.source_id, {})
    current["source_name"] = result.source_name
    current["feed_url"] = result.feed_url
    current["last_checked_at"] = now_text()
    if result.ok:
        current["last_success_at"] = current["last_checked_at"]
        current["last_error"] = ""
        current["last_failed_at"] = current.get("last_failed_at", "")
    else:
        current["last_error"] = clean_health_error(result.error or str(result.status))
        current["last_failed_at"] = current["last_checked_at"]
    source_health[result.source_id] = current
    save_health(health)


def run_fetch_once() -> dict:
    sources = [item for item in normalize_sources() if item.get("enabled", False)]
    settings = load_settings()
    results = fetch_many(sources, settings=settings, timeout=45)

    inserted_total = 0
    success_sources = 0
    failures: list[str] = []

    for result in results:
        update_source_health(result)
        if not result.ok:
            failures.append(f"{result.source_name}: {result.error or result.status}")
            continue
        success_sources += 1
        inserted_total += save_entries(result.entries)

    status = load_status()
    status["last_run_at"] = now_text()
    status["last_success_at"] = status["last_run_at"] if success_sources else status.get("last_success_at", "")
    status["last_total_sources"] = len(sources)
    status["last_success_sources"] = success_sources
    status["last_inserted_entries"] = inserted_total
    status["last_error"] = " | ".join(failures[:10])
    save_status(status)
    return status


def scheduler_loop() -> None:
    while True:
        wait_seconds = max((next_scheduled_run() - datetime.now()).total_seconds(), 1)
        time.sleep(wait_seconds)
        try:
            run_fetch_once()
        except Exception as exc:
            status = load_status()
            status["last_run_at"] = now_text()
            status["last_error"] = str(exc)
            save_status(status)


def should_run_startup_catchup(now: datetime | None = None) -> bool:
    current = now or datetime.now()
    latest_slot = latest_scheduled_run(current)
    if latest_slot is None:
        return False
    last_run_at = to_datetime(load_status().get("last_run_at", ""))
    return last_run_at is None or last_run_at < latest_slot


def nav_link(view: str, params: dict[str, str]) -> str:
    query = {"view": view}
    query.update({k: v for k, v in params.items() if v})
    return "/?" + urlencode(query)


def text_sort_button(view: str, label: str, sort_key: str, current_sort: str, current_dir: str, params: dict[str, str]) -> str:
    next_dir = "asc" if current_sort != sort_key or current_dir == "desc" else "desc"
    mark = ""
    if current_sort == sort_key:
        mark = " A-Z" if current_dir == "asc" else " Z-A"
    href = nav_link(view, params | {"sort": sort_key, "dir": next_dir})
    return f'<a class="sort-btn" href="{html.escape(href)}">{html.escape(label)}{mark}</a>'


def time_sort_button(view: str, label: str, sort_key: str, current_sort: str, current_dir: str, params: dict[str, str]) -> str:
    next_dir = "desc" if current_sort != sort_key or current_dir == "asc" else "asc"
    mark = ""
    if current_sort == sort_key:
        mark = " 新-旧" if current_dir == "desc" else " 旧-新"
    href = nav_link(view, params | {"sort": sort_key, "dir": next_dir})
    return f'<a class="sort-btn" href="{html.escape(href)}">{html.escape(label)}{mark}</a>'


def sort_rows(rows: list[dict], sort_key: str, direction: str) -> list[dict]:
    reverse = direction == "desc"

    def key_func(item: dict):
        value = item.get(sort_key, "")
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            return value
        if value is None:
            return ""
        return str(value).lower()

    return sorted(rows, key=key_func, reverse=reverse)


def render_sidebar(active: str) -> str:
    items = [
        ("entries", "Subscriptions 内容"),
        ("laterhub", "Laterhub 内容"),
        ("settings", "设置"),
    ]
    links = []
    for key, label in items:
        cls = "nav-item active" if key == active else "nav-item"
        links.append(f'<a class="{cls}" href="/?view={key}">{label}</a>')
    return "".join(links)


def render_entries_view(params: dict[str, str], sort_key: str, direction: str) -> str:
    rows = list_entries(limit=500)
    enabled_ids = {item["id"] for item in normalize_sources() if item.get("enabled", False)}
    rows = [item for item in rows if item["source_id"] in enabled_ids]
    query = params.get("q", "")
    if query:
        q = query.lower()
        rows = [
            item
            for item in rows
            if q in item["source_name"].lower() or q in item["title"].lower() or q in item["summary"].lower()
        ]

    for item in rows:
        item["display_time"] = format_datetime(item["published"] or item["created_at"])
        item["sort_time"] = sortable_datetime(item["published"] or item["created_at"])

    sort_key = sort_key or "sort_time"
    direction = direction or "desc"
    rows = sort_rows(rows, sort_key, direction)

    body = []
    for item in rows:
        body.append(
            "<tr>"
            f"<td>{html.escape(item['display_time'])}</td>"
            f"<td>{html.escape(item['source_name'])}</td>"
            f"<td><a href=\"{html.escape(item['link'])}\" target=\"_blank\">{html.escape(item['title'])}</a></td>"
            "</tr>"
        )

    sort_params = {"q": query}
    return (
        "<section class='card'>"
        "<div class='panel-head'>"
        "<h2>Subscriptions 内容</h2>"
        "<form method='get' class='inline-form'>"
        "<input type='hidden' name='view' value='entries'>"
        f"<input type='hidden' name='sort' value='{html.escape(sort_key)}'>"
        f"<input type='hidden' name='dir' value='{html.escape(direction)}'>"
        f"<input class='search' name='q' value='{html.escape(query)}' placeholder='搜索标题、来源、摘要'>"
        "<button class='btn' type='submit'>搜索</button>"
        "</form>"
        "</div>"
        "<table><thead><tr>"
        f"<th>{time_sort_button('entries', '时间', 'sort_time', sort_key, direction, sort_params)}</th>"
        f"<th>{text_sort_button('entries', '来源', 'source_name', sort_key, direction, sort_params)}</th>"
        f"<th>{text_sort_button('entries', '标题', 'title', sort_key, direction, sort_params)}</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
        "</section>"
    )


def render_laterhub_view(params: dict[str, str], sort_key: str, direction: str) -> str:
    rows = list_laterhub_items(limit=500)
    query = normalize_text(params.get("q", ""))
    filter_finished = params.get("filter_finished", "0")
    selected_tags = split_tag_values(params.get("filter_tag", ""))
    selected_tag_keys = {normalize_text(tag) for tag in selected_tags}

    for item in rows:
        item["display_time"] = format_datetime(item["created_at"])
        item["sort_time"] = sortable_datetime(item["created_at"])
        item["finished_text"] = "已完成" if item.get("is_finished") else "未完成"
        item["tags_text"] = item.get("tags") or ""
        item["tag_list"] = split_tag_values(item["tags_text"])
        item["tag_keys"] = {normalize_text(tag) for tag in item["tag_list"]}

    if query:
        rows = [item for item in rows if query in normalize_text(item["title"]) or query in normalize_text(item["tags_text"])]
    if filter_finished == "1":
        rows = [item for item in rows if item.get("is_finished")]
    elif filter_finished == "0":
        rows = [item for item in rows if not item.get("is_finished")]
    if selected_tag_keys:
        rows = [item for item in rows if selected_tag_keys.issubset(item["tag_keys"])]

    sort_key = sort_key or "sort_time"
    direction = direction or "desc"
    rows = sort_rows(rows, sort_key, direction)
    all_tags = sorted(
        {
            tag
            for item in list_laterhub_items(limit=500)
            for tag in split_tag_values(item.get("tags") or "")
            if tag.strip()
        },
        key=lambda value: value.lower(),
    )
    sort_params = {
        "q": params.get("q", ""),
        "filter_finished": filter_finished,
        "filter_tag": join_tag_values(selected_tags),
    }

    body = []
    for item in rows:
        checked = "checked" if item.get("is_finished") else ""
        body.append(
            "<tr>"
            f"<td>{html.escape(item['display_time'])}</td>"
            f"<td><div><a href=\"{html.escape(item['url'])}\" target=\"_blank\" rel=\"noreferrer\">{html.escape(item['title'])}</a></div><div class='subtle'>{html.escape(item['tags_text'])}</div></td>"
            "<td>"
            "<form method='post' action='/laterhub-finish'>"
            f"<input type='hidden' name='id' value='{item['id']}'>"
            "<input type='hidden' name='view' value='laterhub'>"
            f"<input type='hidden' name='q' value='{html.escape(params.get('q', ''))}'>"
            f"<input type='hidden' name='filter_finished' value='{html.escape(filter_finished)}'>"
            f"<input type='hidden' name='filter_tag' value='{html.escape(params.get('filter_tag', ''))}'>"
            f"<input type='hidden' name='sort' value='{html.escape(sort_key)}'>"
            f"<input type='hidden' name='dir' value='{html.escape(direction)}'>"
            f"<input type='checkbox' name='finished' value='1' {checked} onchange='this.form.submit()'>"
            "</form>"
            "</td>"
            "</tr>"
        )

    tag_links = [f"<a class='tag-chip{' active' if not selected_tags else ''}' href='{html.escape(nav_link('laterhub', sort_params | {'filter_tag': ''}))}'>全部</a>"]
    for tag in all_tags:
        tag_key = normalize_text(tag)
        if tag_key in selected_tag_keys:
            next_tags = [item for item in selected_tags if normalize_text(item) != tag_key]
            active = " active"
        else:
            next_tags = [*selected_tags, tag]
            active = ""
        href = nav_link("laterhub", sort_params | {"filter_tag": join_tag_values(next_tags)})
        tag_links.append(f"<a class='tag-chip{active}' href='{html.escape(href)}'>{html.escape(tag)}</a>")

    return (
        "<section class='card'>"
        "<div class='panel-head'><h2>Laterhub 内容</h2></div>"
        "<form method='get' class='filter-grid laterhub-toolbar'>"
        "<input type='hidden' name='view' value='laterhub'>"
        f"<input type='hidden' name='sort' value='{html.escape(sort_key)}'>"
        f"<input type='hidden' name='dir' value='{html.escape(direction)}'>"
        "<div class='keyword-field'>"
        "<label>关键词"
        f"<div class='keyword-search'><input name='q' value='{html.escape(params.get('q', ''))}' placeholder='搜索标题或标签'><button class='btn' type='submit'>搜索</button></div>"
        "</label>"
        "</div>"
        "<label class='finished-field'>完成状态<select name='filter_finished' onchange='this.form.requestSubmit()'>"
        f"<option value='0' {'selected' if filter_finished == '0' else ''}>未完成</option>"
        f"<option value='1' {'selected' if filter_finished == '1' else ''}>已完成</option>"
        f"<option value='' {'selected' if not filter_finished else ''}>全部</option>"
        "</select></label>"
        "<div class='filter-actions'>"
        "<a class='btn ghost' href='/?view=laterhub'>清空</a>"
        "</div>"
        "</form>"
        f"<div class='tag-summary'>已选标签：{html.escape('、'.join(selected_tags) if selected_tags else '无')}</div>"
        f"<div class='tag-strip'>{''.join(tag_links)}</div>"
        "<table><thead><tr>"
        f"<th>{time_sort_button('laterhub', '时间', 'sort_time', sort_key, direction, sort_params)}</th>"
        f"<th>{text_sort_button('laterhub', '链接', 'title', sort_key, direction, sort_params)}</th>"
        f"<th>{text_sort_button('laterhub', '完成', 'finished_text', sort_key, direction, sort_params)}</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
        "</section>"
    )


def source_rows_for_settings(sort_key: str, direction: str, query: str) -> list[dict]:
    stats = {item["source_id"]: item for item in list_source_stats()}
    health = load_health().get("sources", {})
    rows = []
    for item in normalize_sources():
        if query:
            q = query.lower()
            if q not in item["name"].lower() and q not in item["feed_url"].lower():
                continue
        stat = stats.get(item["id"], {})
        source_health = health.get(item["id"], {})
        failed_at = to_datetime(source_health.get("last_failed_at", ""))
        success_at = to_datetime(source_health.get("last_success_at", ""))
        invalid_days = ""
        if failed_at and (not success_at or failed_at > success_at):
            invalid_days = str(max((datetime.now() - failed_at).days, 0))
        rows.append(
            {
                "id": item["id"],
                "name": item["name"],
                "provider_label": provider_label(item),
                "enabled": bool(item.get("enabled", True)),
                "enabled_text": "生效" if item.get("enabled", True) else "停用",
                "enabled_sort": 1 if item.get("enabled", True) else 0,
                "entry_count": stat.get("entry_count", 0),
                "invalid_days": invalid_days,
                "invalid_text": invalid_days or "-",
                "invalid_sort": int(invalid_days) if invalid_days else -1,
                "site_url": item.get("site_url", ""),
                "feed_url": item.get("feed_url", ""),
            }
        )
    sort_key = sort_key or "name"
    direction = direction or "asc"
    return sort_rows(rows, sort_key, direction)


def render_sources_management(params: dict[str, str], sort_key: str, direction: str) -> str:
    query = params.get("source_q", "")
    rows = source_rows_for_settings(sort_key, direction, query)
    normalized_query = normalize_text(query)
    edit_id = params.get("edit_source", "")

    if normalized_query:
        rows = [
            item
            for item in rows
            if normalized_query in normalize_text(item["name"])
            or normalized_query in normalize_text(item["feed_url"])
            or normalized_query in normalize_text(item.get("site_url", ""))
        ]

    rows = sorted(rows, key=lambda item: sortable_source_value(item, sort_key), reverse=direction == "desc")
    editing = next((item for item in rows if item["id"] == edit_id), None)
    sort_params = {"source_q": query}
    body = []
    for item in rows:
        invalid_text = item["invalid_text"]
        edit_href = nav_link("settings", {"edit_source": item["id"], "source_q": query, "sort": sort_key, "dir": direction})
        body.append(
            "<tr>"
            f"<td>{html.escape(item['name'])}</td>"
            f"<td>{html.escape(item['enabled_text'])}</td>"
            f"<td>{html.escape(item['provider_label'])}</td>"
            f"<td>{item['entry_count']}</td>"
            f"<td>{html.escape(invalid_text)}</td>"
            "<td>"
            f"<a href='/source-toggle?id={html.escape(item['id'])}&enabled={'0' if item['enabled'] else '1'}' onclick=\"return confirm('{html.escape('确认停用该订阅源？' if item['enabled'] else '确认启用该订阅源？')}')\">{'停用' if item['enabled'] else '生效'}</a> / "
            f"<a href='{html.escape(edit_href)}'>??</a> / "
            f"<a href='/source-delete?id={html.escape(item['id'])}' onclick=\"return confirm('?????')\">??</a>"
            "</td>"
            "</tr>"
        )

    modal = ""
    if editing:
        modal = (
            "<div class='modal-backdrop'>"
            "<div class='modal-card'>"
            "<div class='panel-head'><h3>?????</h3><a class='modal-close' href='/?view=settings'>??</a></div>"
            "<form method='post' action='/source-save' class='form-grid'>"
            f"<input type='hidden' name='source_id' value='{html.escape(editing['id'])}'>"
            f"<label>????<input name='name' value='{html.escape(editing.get('name', ''))}' required></label>"
            f"<label>?? URL<input name='site_url' value='{html.escape(editing.get('site_url', ''))}'></label>"
            f"<label>RSS URL<input name='feed_url' value='{html.escape(editing.get('feed_url', ''))}' required></label>"
            "<div class='modal-actions'>"
            "<button class='btn' type='submit'>??</button>"
            "<a class='btn ghost' href='/?view=settings'>??</a>"
            "</div>"
            "</form>"
            "</div>"
            "</div>"
        )

    return (
        "<section class='card'>"
        "<div class='panel-head'>"
        "<h2>?????</h2>"
        "<form method='get' class='inline-form'>"
        "<input type='hidden' name='view' value='settings'>"
        f"<input type='hidden' name='sort' value='{html.escape(sort_key)}'>"
        f"<input type='hidden' name='dir' value='{html.escape(direction)}'>"
        f"<input class='search' name='source_q' value='{html.escape(query)}' placeholder='????? RSS URL'>"
        "<button class='btn' type='submit'>??</button>"
        "</form>"
        "</div>"
        "<table><thead><tr>"
        f"<th>{text_sort_button('settings', '??', 'name', sort_key, direction, sort_params)}</th>"
        f"<th>{text_sort_button('settings', '生效', 'enabled_sort', sort_key, direction, sort_params)}</th>"
        f"<th>{text_sort_button('settings', '????', 'provider_label', sort_key, direction, sort_params)}</th>"
        f"<th>{time_sort_button('settings', '????', 'entry_count', sort_key, direction, sort_params)}</th>"
        f"<th>{time_sort_button('settings', '????', 'invalid_sort', sort_key, direction, sort_params)}</th>"
        "<th>??/??</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
        "</section>"
        f"{modal}"
    )


def render_settings_view(params: dict[str, str], sort_key: str, direction: str) -> str:
    status = load_status()
    summary = list_laterhub_summary()
    laterhub_source_rows = list_laterhub_source_stats()
    sources_block = render_sources_management(params, sort_key or 'name', direction or 'asc')
    error_text = status.get('last_error', '') or '?'
    laterhub_source_body = []
    for item in laterhub_source_rows:
        laterhub_source_body.append(
            '<tr>'
            f"<td>{html.escape(item['label'])}</td>"
            f"<td>{html.escape(item['purpose'])}</td>"
            f"<td>{html.escape(item['fetch_mode'])}</td>"
            f"<td>{item['total_count']}</td>"
            f"<td>{item['unfinished_count']}</td>"
            '</tr>'
        )
    return (
        "<section class='settings-grid'>"
        "<section class='card'>"
        "<h2>????</h2>"
        f"<p>?????http://{HOST}:{PORT}</p>"
        f"<p>?????{html.escape(format_datetime(status.get('last_run_at', '')))}</p>"
        f"<p>?????{html.escape(format_datetime(status.get('last_success_at', '')))}</p>"
        f"<p>????{status.get('last_total_sources', 0)} / ?????{status.get('last_success_sources', 0)}</p>"
        f"<p>?????{status.get('last_inserted_entries', 0)}</p>"
        "</section>"
        "<section class='card fetch-card'>"
        "<h2>????</h2>"
        "<p>?? 06:00?16:00 ??????????????????????????????????</p>"
        "<div class='actions'>"
        "<a class='btn' href='/fetch-now'>??????</a>"
        "</div>"
        "</section>"
        "<section class='card'>"
        "<details>"
        "<summary>????</summary>"
        f"<pre class='error-box'>{html.escape(error_text)}</pre>"
        "</details>"
        "</section>"
        "<section class='card'>"
        "<h2>Laterhub ??</h2>"
        f"<p>???{summary.get('total_count', 0)}</p>"
        f"<p>????{summary.get('unfinished_count', 0)}</p>"
        f"<p>????{summary.get('finished_count', 0)}</p>"
        "</section>"
        "<section class='card'>"
        "<h2>????</h2>"
        "<p>?????????????????????</p>"
        "<p>Subscriptions????????? RSS / ???? / ?????????????? `rss.sqlite3`?</p>"
        "<p>Laterhub?????????????? API / ??????? / ??????????? `info_hub.db`?</p>"
        "<p>Newshub???????????????????????????????????</p>"
        "<p>??????????????????????????????????????????? profile?</p>"
        "</section>"
        "<section class='card'>"
        "<h2>Laterhub ????</h2>"
        "<table><thead><tr>"
        "<th>??</th><th>????</th><th>????</th><th>??</th><th>???</th>"
        "</tr></thead>"
        f"<tbody>{''.join(laterhub_source_body)}</tbody></table>"
        "</section>"
        f"{sources_block}"
        "</section>"
    )

def page_html(view: str, params: dict[str, str]) -> str:
    sort_key = params.get("sort", "")
    direction = params.get("dir", "")

    if view == "laterhub":
        content = render_laterhub_view(params, sort_key, direction)
    elif view == "settings":
        content = render_settings_view(params, sort_key, direction)
    else:
        content = render_entries_view(params, sort_key, direction)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>infoflow</title>
  <style>
    :root {{
      --bg: #efe7db;
      --panel: #fcf8f1;
      --panel-strong: #fffdf9;
      --line: #d7c8b0;
      --text: #2d281f;
      --muted: #766956;
      --accent: #8f4f22;
      --accent-soft: #f0dfcd;
      --shadow: 0 16px 40px rgba(88, 62, 31, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(255,255,255,0.75), transparent 30%),
        linear-gradient(180deg, #f8f2e8 0%, var(--bg) 100%);
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .app {{
      display: grid;
      grid-template-columns: 280px 1fr;
      min-height: 100vh;
    }}
    .sidebar {{
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 24px 18px;
      border-right: 1px solid var(--line);
      background: rgba(255,255,255,0.42);
      backdrop-filter: blur(8px);
    }}
    .brand {{
      margin: 0 0 18px;
      font-size: 30px;
      font-weight: 900;
      letter-spacing: 1px;
    }}
    .nav {{
      display: grid;
      gap: 10px;
    }}
    .nav-item {{
      display: block;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      color: var(--text);
      background: rgba(255,255,255,0.55);
    }}
    .nav-item.active {{
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }}
    .main {{
      padding: 26px;
    }}
    .card {{
      background: rgba(252,248,241,0.92);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
      padding: 18px 18px 16px;
      margin-bottom: 18px;
    }}
    .panel-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .panel-head h2, .panel-head h3 {{
      margin: 0;
      font-size: 20px;
    }}
    .inline-form, .filter-grid {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: end;
    }}
    .filter-grid label, .form-grid label {{
      display: grid;
      gap: 6px;
      min-width: 180px;
      color: var(--muted);
      font-size: 13px;
    }}
    .three-col label {{
      min-width: 220px;
    }}
    .laterhub-toolbar {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 220px auto;
      align-items: end;
    }}
    .keyword-field {{
      min-width: 0;
    }}
    .keyword-search {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }}
    .finished-field {{
      min-width: 180px;
      justify-self: end;
    }}
    input, select {{
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fffdf9;
      color: var(--text);
      font: inherit;
    }}
    .search {{
      min-width: 260px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 10px 14px;
      border-radius: 12px;
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      font: inherit;
      text-decoration: none;
    }}
    .btn.ghost {{
      color: var(--accent);
      background: transparent;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      margin-top: 12px;
    }}
    .filter-actions {{
      display: flex;
      gap: 10px;
      justify-self: end;
    }}
    .tag-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 14px 0 8px;
    }}
    .tag-chip {{
      display: inline-flex;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.72);
      color: var(--text);
      text-decoration: none;
      font-size: 13px;
      line-height: 1;
    }}
    .tag-chip.active {{
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }}
    .tag-summary {{
      margin: 6px 0 2px;
      color: var(--muted);
      font-size: 13px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      vertical-align: top;
      padding: 12px 10px;
      border-top: 1px solid rgba(215,200,176,0.8);
      font-size: 14px;
    }}
    thead th {{
      border-top: none;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .sort-btn {{
      color: inherit;
      text-decoration: none;
      white-space: nowrap;
    }}
    .sort-btn:hover {{
      color: var(--accent);
    }}
    .subtle {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
    }}
    .settings-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }}
    .settings-grid > .card:last-child {{
      grid-column: 1 / -1;
    }}
    .fetch-card {{
      align-self: start;
    }}
    details summary {{
      cursor: pointer;
      color: var(--accent);
      font-weight: 700;
    }}
    .error-box {{
      margin: 12px 0 0;
      padding: 12px;
      border-radius: 14px;
      background: #fff7f2;
      border: 1px solid #e2cbb5;
      white-space: pre-wrap;
      word-break: break-word;
      color: #7a3f25;
      font-size: 12px;
    }}
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(34, 28, 21, 0.35);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .modal-card {{
      width: min(640px, 100%);
      background: var(--panel-strong);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: 0 26px 60px rgba(46, 36, 22, 0.18);
      padding: 18px;
    }}
    .modal-close {{
      color: var(--muted);
    }}
    .form-grid {{
      display: grid;
      gap: 12px;
    }}
    .modal-actions {{
      display: flex;
      gap: 10px;
      justify-content: flex-end;
      margin-top: 4px;
    }}
    @media (max-width: 980px) {{
      .app {{
        grid-template-columns: 1fr;
      }}
      .sidebar {{
        position: static;
        height: auto;
        border-right: none;
        border-bottom: 1px solid var(--line);
      }}
      .settings-grid {{
        grid-template-columns: 1fr;
      }}
      .laterhub-toolbar {{
        grid-template-columns: 1fr;
      }}
      .finished-field, .filter-actions {{
        justify-self: stretch;
      }}
    }}
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">infoflow</div>
      <nav class="nav">{render_sidebar(view)}</nav>
    </aside>
    <main class="main">{content}</main>
  </div>
  <script>
    (function () {{
      function shouldHandlePanelUrl(rawUrl) {{
        try {{
          const url = new URL(rawUrl, window.location.href);
          return url.origin === window.location.origin;
        }} catch {{
          return false;
        }}
      }}

      function isExternalLikeLink(link) {{
        return link.hasAttribute("download") || link.target === "_blank" || link.getAttribute("rel") === "external";
      }}

      async function replacePanelContent(response, fallbackUrl) {{
        const text = await response.text();
        const doc = new DOMParser().parseFromString(text, "text/html");
        const nextMain = doc.querySelector(".main");
        const nextNav = doc.querySelector(".nav");
        const currentMain = document.querySelector(".main");
        const currentNav = document.querySelector(".nav");
        if (!nextMain || !currentMain || !nextNav || !currentNav) {{
          window.location.href = response.url || fallbackUrl;
          return;
        }}
        currentMain.innerHTML = nextMain.innerHTML;
        currentNav.innerHTML = nextNav.innerHTML;
        if (doc.title) {{
          document.title = doc.title;
        }}
        history.replaceState(null, "", response.url || fallbackUrl);
      }}

      async function handlePanelNavigation(url, options) {{
        const response = await fetch(url, {{
          ...options,
          headers: {{
            "X-Requested-With": "XMLHttpRequest",
            ...(options.headers || {{}}),
          }},
          redirect: "follow",
        }});
        if (!response.ok) {{
          window.location.href = url;
          return;
        }}
        await replacePanelContent(response, url);
      }}

      document.addEventListener("click", function (event) {{
        if (event.defaultPrevented) {{
          return;
        }}
        const link = event.target.closest("a[href]");
        if (!link) {{
          return;
        }}
        if (!link.closest(".app")) {{
          return;
        }}
        if (isExternalLikeLink(link)) {{
          return;
        }}
        const href = link.getAttribute("href");
        if (!href || !shouldHandlePanelUrl(href)) {{
          return;
        }}
        event.preventDefault();
        handlePanelNavigation(link.href, {{ method: "GET" }}).catch(function () {{
          window.location.href = link.href;
        }});
      }});

      document.addEventListener("submit", function (event) {{
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.closest(".app")) {{
          return;
        }}
        const method = (form.getAttribute("method") || "get").toUpperCase();
        const action = form.getAttribute("action") || window.location.href;
        if (!shouldHandlePanelUrl(action)) {{
          return;
        }}
        event.preventDefault();
        const formData = new FormData(form);
        if (method === "GET") {{
          const url = new URL(action, window.location.href);
          url.search = new URLSearchParams(formData).toString();
          handlePanelNavigation(url.toString(), {{ method: "GET" }}).catch(function () {{
            window.location.href = url.toString();
          }});
          return;
        }}
        handlePanelNavigation(action, {{
          method,
          headers: {{
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
          }},
          body: new URLSearchParams(formData).toString(),
        }}).catch(function () {{
          form.submit();
        }});
      }});
    }})();
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send_text(self, text: str, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}

        if parsed.path == "/fetch-now":
            run_fetch_once()
            self._redirect("/?view=settings")
            return

        if parsed.path == "/source-delete":
            source_id = params.get("id", "")
            if source_id:
                delete_source(source_id)
            self._redirect("/?view=settings")
            return

        if parsed.path == "/source-toggle":
            source_id = params.get("id", "")
            enabled = params.get("enabled", "1") == "1"
            if source_id:
                update_source_enabled(source_id, enabled)
            self._redirect("/?view=settings")
            return

        if parsed.path == "/healthz":
            self._send_text("ok", content_type="text/plain; charset=utf-8")
            return

        view = params.get("view", "entries")
        self._send_text(page_html(view, params))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        form = parse_qs(raw)

        if parsed.path == "/source-save":
            save_source_from_form(form)
            self._redirect("/?view=settings")
            return

        if parsed.path == "/laterhub-finish":
            link_id = int(form.get("id", ["0"])[0])
            finished = form.get("finished", ["0"])[0] == "1"
            if link_id:
                mark_laterhub_finished(link_id, finished)
            params = {
                "view": "laterhub",
                "q": form.get("q", [""])[0],
                "filter_finished": form.get("filter_finished", [""])[0],
                "filter_tag": form.get("filter_tag", [""])[0],
                "sort": form.get("sort", [""])[0],
                "dir": form.get("dir", [""])[0],
            }
            self._redirect(nav_link("laterhub", params))
            return

        self._send_text("not found", content_type="text/plain; charset=utf-8", status=404)


def main() -> int:
    normalize_sources()
    ensure_runtime()
    health = load_health()
    source_map = health.setdefault("sources", {})
    for source in normalize_sources():
        source_map.setdefault(
            source["id"],
            {
                "source_name": source["name"],
                "feed_url": source["feed_url"],
                "last_checked_at": "",
                "last_success_at": "",
                "last_failed_at": "",
                "last_error": "",
            },
        )
    save_health(health)
    if should_run_startup_catchup():
        try:
            run_fetch_once()
        except Exception as exc:
            status = load_status()
            status["last_run_at"] = now_text()
            status["last_error"] = str(exc)
            save_status(status)
    worker = threading.Thread(target=scheduler_loop, daemon=True)
    worker.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"infoflow running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

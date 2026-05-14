from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors.auth.providers.bilibili import get_bilibili_cookie

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
BILIBILI_API_CANDIDATES = [
    "https://api.bilibili.com/x/v2/history/toview/web",
    "https://api.bilibili.com/x/v2/history/toview",
]
BILIBILI_DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"


def load_project_env(env_path: str | Path | None = None) -> None:
    if load_dotenv is None:
        return
    target = Path(env_path) if env_path else DEFAULT_ENV_PATH
    if target.exists():
        load_dotenv(dotenv_path=target, override=False)


@dataclass(slots=True)
class BilibiliConfig:
    cookie: str
    ps: int = 20

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "BilibiliConfig":
        load_project_env(env_path)
        cookie = get_bilibili_cookie()
        if not cookie:
            raise ValueError("缺少 B 站登录态。请在 .env 中填写 BILIBILI_COOKIE，或至少填写 BILIBILI_SESSDATA")
        return cls(cookie=cookie)


class BilibiliWatchLaterFetcher:
    def __init__(self, config: BilibiliConfig, timeout: int = 20) -> None:
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/watchlater/list",
                "Cookie": self.config.cookie,
            }
        )

    def fetch(self) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for url in BILIBILI_API_CANDIDATES:
            try:
                response = self.session.get(url, params={"ps": self.config.ps}, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"B 站接口返回异常: {data}")
                items = self._extract_items(data)
                if items is None:
                    raise RuntimeError(f"无法识别 B 站稍后看返回结构: {data}")
                return items
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"获取 B 站稍后看失败: {last_error}")

    @staticmethod
    def _extract_items(data: dict[str, Any]) -> list[dict[str, Any]] | None:
        container = data.get("data")
        if isinstance(container, dict):
            for key in ("list", "items"):
                if isinstance(container.get(key), list):
                    return [BilibiliWatchLaterFetcher._normalize_item(x) for x in container[key]]
            if isinstance(container.get("list"), dict) and isinstance(container["list"].get("list"), list):
                return [BilibiliWatchLaterFetcher._normalize_item(x) for x in container["list"]["list"]]
        if isinstance(container, list):
            return [BilibiliWatchLaterFetcher._normalize_item(x) for x in container]
        return None

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
        bvid = item.get("bvid") or item.get("bv_id") or ""
        aid = item.get("aid") or item.get("id")
        title = item.get("title") or item.get("page_title") or "未命名视频"
        if bvid:
            url = f"https://www.bilibili.com/video/{bvid}"
        elif aid:
            url = f"https://www.bilibili.com/video/av{aid}"
        else:
            url = item.get("short_link_v2") or item.get("uri") or ""
        owner = item.get("owner") or {}
        owner_name = owner.get("name") or item.get("author") or ""
        return {
            "url": url,
            "title": title,
            "source": "bilibili_watchlater",
            "owner": owner_name,
            "tags": None,
            "raw": item,
        }


def fetch_bilibili_watchlater(env_path: str | Path | None = None) -> list[dict[str, Any]]:
    config = BilibiliConfig.from_env(env_path)
    return BilibiliWatchLaterFetcher(config).fetch()


class BilibiliApiSession:
    def __init__(self, config: BilibiliConfig, timeout: int = 20) -> None:
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
                ),
                "Origin": "https://space.bilibili.com",
                "Cookie": self.config.cookie,
            }
        )

    def get(self, url: str, *, params: dict[str, Any], referer: str) -> dict[str, Any]:
        headers = {"Referer": referer}
        response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"B站接口返回异常: code={data.get('code')} message={data.get('message')}")
        return data


def build_bilibili_dynamic_link(item: dict[str, Any]) -> str:
    dynamic_id = str(item.get("id_str") or item.get("id") or "").strip()
    if not dynamic_id:
        return ""
    return f"https://t.bilibili.com/{dynamic_id}"


def pick_bilibili_dynamic_link(item: dict[str, Any]) -> str:
    basic = item.get("basic") or {}
    major = ((item.get("modules") or {}).get("module_dynamic") or {}).get("major") or {}
    for value in (
        basic.get("jump_url"),
        (major.get("opus") or {}).get("jump_url"),
        (major.get("archive") or {}).get("jump_url"),
        (major.get("article") or {}).get("jump_url"),
        (major.get("common") or {}).get("jump_url"),
        (major.get("music") or {}).get("jump_url"),
        (major.get("pgc") or {}).get("jump_url"),
        (major.get("medialist") or {}).get("jump_url"),
        (major.get("ugc_season") or {}).get("jump_url"),
        (major.get("live") or {}).get("jump_url"),
        (major.get("live_rcmd") or {}).get("jump_url"),
    ):
        text = str(value or "").strip()
        if not text:
            continue
        if text.startswith("//"):
            return f"https:{text}"
        if text.startswith("/"):
            return f"https://www.bilibili.com{text}"
        return text
    return build_bilibili_dynamic_link(item)


def extract_bilibili_dynamic_text(item: dict[str, Any]) -> str:
    module_dynamic = (item.get("modules") or {}).get("module_dynamic") or {}
    major = module_dynamic.get("major") or {}
    candidates = [
        ((module_dynamic.get("desc") or {}).get("text") if isinstance(module_dynamic.get("desc"), dict) else ""),
        (major.get("opus") or {}).get("title"),
        ((major.get("opus") or {}).get("summary") or {}).get("text") if isinstance((major.get("opus") or {}).get("summary"), dict) else "",
        (major.get("archive") or {}).get("title"),
        (major.get("archive") or {}).get("desc"),
        (major.get("article") or {}).get("title"),
        (major.get("article") or {}).get("desc"),
        (major.get("common") or {}).get("title"),
        (major.get("common") or {}).get("desc"),
        (major.get("music") or {}).get("title"),
        (major.get("music") or {}).get("desc"),
        ((module_dynamic.get("additional") or {}).get("desc") or {}).get("text")
        if isinstance((module_dynamic.get("additional") or {}).get("desc"), dict)
        else "",
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def format_bilibili_published(published_ts: int, published_text: str) -> str:
    if published_ts > 0:
        return datetime.fromtimestamp(published_ts).strftime("%Y/%m/%d %H:%M")
    return str(published_text or "").strip()


def fetch_bilibili_user_dynamic(
    uid: str,
    *,
    limit: int = 12,
    timeout: int = 20,
    max_pages: int = 2,
    retry_delays: tuple[float, ...] = (1.5, 3.0, 6.0),
    env_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    load_project_env(env_path)
    cookie = get_bilibili_cookie()
    config = BilibiliConfig(cookie=cookie) if cookie else BilibiliConfig.from_env(env_path)
    client = BilibiliApiSession(config=config, timeout=timeout)
    referer = f"https://space.bilibili.com/{uid}/dynamic"
    offset = ""
    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for _ in range(max_pages):
        params = {
            "host_mid": uid,
            "offset": offset,
            "timezone_offset": -480,
            "features": "itemOpusStyle",
        }
        last_error: Exception | None = None
        payload: dict[str, Any] | None = None
        for attempt, delay in enumerate((0.0, *retry_delays), start=1):
            if delay:
                time.sleep(delay)
            try:
                payload = client.get(BILIBILI_DYNAMIC_API, params=params, referer=referer)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == len(retry_delays) + 1:
                    raise RuntimeError(f"B站动态接口失败[uid={uid}][attempt={attempt}]: {exc}") from exc
        if payload is None:
            raise RuntimeError(f"B站动态接口失败[uid={uid}]: {last_error}")

        data = payload.get("data") or {}
        items = data.get("items") or []
        for item in items:
            dynamic_id = str(item.get("id_str") or item.get("id") or "").strip()
            if not dynamic_id or dynamic_id in seen_ids:
                continue
            seen_ids.add(dynamic_id)
            author = (item.get("modules") or {}).get("module_author") or {}
            published_ts = int(author.get("pub_ts") or 0)
            published_text = str(author.get("pub_time") or "").strip()
            action = str(author.get("pub_action") or "").strip()
            title = extract_bilibili_dynamic_text(item)
            link = pick_bilibili_dynamic_link(item)
            collected.append(
                {
                    "id": dynamic_id,
                    "type": str(item.get("type") or "").strip(),
                    "title": title,
                    "summary": action,
                    "link": link,
                    "dynamic_link": build_bilibili_dynamic_link(item),
                    "published_ts": published_ts,
                    "published_at": format_bilibili_published(published_ts, published_text),
                    "published_text": published_text,
                    "author": str(author.get("name") or "").strip(),
                    "raw": item,
                }
            )
            if len(collected) >= limit:
                return collected

        offset = str(data.get("offset") or "").strip()
        has_more = bool(data.get("has_more"))
        if not has_more or not offset:
            break

    return collected[:limit]


def fetch_bilibili_dynamic_feed(source: dict, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    site_url = str(source.get("site_url") or "").strip()
    match = re.search(r"space\.bilibili\.com/(\d+)", site_url)
    if not match:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source["feed_url"],
            ok=False,
            status=0,
            entries=[],
            error="暂不支持的 B 站网页源",
        )

    uid = match.group(1)
    page_url = f"https://space.bilibili.com/{uid}/dynamic"
    try:
        items = fetch_bilibili_user_dynamic(uid, limit=limit, timeout=max(10, timeout_ms // 1000))
    except Exception as exc:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=page_url,
            ok=False,
            status=0,
            entries=[],
            error=f"B站抓取失败[uid={uid}][stage=api][kind=unknown][attempt=1][url={page_url}]: {exc}",
        )

    entries: list[FeedEntry] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        link = str(item.get("link") or item.get("dynamic_link") or page_url).strip()
        summary = str(item.get("summary") or "").strip()
        published = str(item.get("published_at") or item.get("published_text") or "").strip()
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=published or datetime.now().strftime("%Y/%m/%d %H:%M"),
                summary=summary,
            )
        )

    if not entries:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=page_url,
            ok=False,
            status=0,
            entries=[],
            error=f"B站抓取失败[uid={uid}][stage=api][kind=empty][attempt=1][url={page_url}]: 动态接口返回成功，但未产出条目",
        )

    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )

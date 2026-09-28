from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.bsk_session import BskRunnerSession
from connectors.auth.providers.bilibili import get_bilibili_cookie

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
# B 站稍后看：bsk 无法手设 Origin/Referer（forbidden header），故先导航到同域页面建立上下文，
# 再在页内用浏览器自带真实 cookie（credentials:include）直接 fetch 官方接口。
BILIBILI_WATCHLATER_PAGE_URL = "https://www.bilibili.com/watchlater/list"
# 与迁移前的 HTTP 路径保持一致：只取第一页，本轮不做翻页扩展。
BILIBILI_WATCHLATER_PS = 20
BILIBILI_DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
# 页内 fetch 一次跑 2 页请求：预算必须明显大于 30s「疑似登录阻塞」看门狗，否则会被误判成 BskHelpTimeout。
BILIBILI_BSK_EVALUATE_TIMEOUT_SECONDS = 120.0
# 单次 fetch 的页内超时（毫秒）：任一页卡住时快速失败，而不是拖满整个 evaluate 预算。
BILIBILI_PAGE_REQUEST_TIMEOUT_MS = 20000


def _bilibili_uid_from_source(source: dict[str, Any]) -> str:
    match = re.search(r"space\.bilibili\.com/(\d+)", str(source.get("site_url") or ""))
    return match.group(1) if match else ""


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


def _extract_watchlater_items(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """从稍后看接口载荷里取出条目并归一化；结构无法识别返回 None。

    稍后看已改为 bsk 页内 fetch 一条取数路径，此处是该路径唯一的解析入口（SSOT）。
    """
    container = data.get("data")
    if isinstance(container, dict):
        for key in ("list", "items"):
            if isinstance(container.get(key), list):
                return [_normalize_watchlater_item(x) for x in container[key]]
        if isinstance(container.get("list"), dict) and isinstance(container["list"].get("list"), list):
            return [_normalize_watchlater_item(x) for x in container["list"]["list"]]
    if isinstance(container, list):
        return [_normalize_watchlater_item(x) for x in container]
    return None


def _normalize_watchlater_item(item: dict[str, Any]) -> dict[str, Any]:
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


# 稍后看接口返回的是 data.list 数组，量级小（ps=20），直接原样回传 data 交给 Python 侧
# 共用归一化逻辑即可，无需像动态流那样做字段投影。
_BILIBILI_WATCHLATER_PAGE_JS = r"""
async (argsJson) => {
  const args = JSON.parse(argsJson);
  const url = new URL("https://api.bilibili.com/x/v2/history/toview/web");
  url.searchParams.set("ps", String(args.ps));
  let resp;
  try {
    resp = await fetch(url.toString(), {
      credentials: "include",
      signal: AbortSignal.timeout(args.requestTimeoutMs),
    });
  } catch (e) {
    return {error: "接口请求失败: " + String(e)};
  }
  let data;
  try {
    data = await resp.json();
  } catch (e) {
    return {error: "接口返回非 JSON（可能登录态失效）"};
  }
  return {code: data.code, message: data.message, data: data.data};
}
"""


def fetch_bilibili_watchlater(
    env_path: str | Path | None = None, *, session: BskRunnerSession | None = None
) -> list[dict[str, Any]]:
    """抓取 B 站「稍后看」列表：bsk 页内 fetch，复用用户浏览器真实登录态。

    env_path 仅为签名兼容保留（pipeline 以 fetcher(ENV_PATH) 调用），内部已不再读取
    .env 的 B 站 cookie。session 为 None 时自建 BskRunnerSession 并负责其生命周期；
    外部传入则只使用、绝不 shutdown（会话归调用方所有）。
    """
    page_url = BILIBILI_WATCHLATER_PAGE_URL
    own_session = session is None
    if session is None:
        session = BskRunnerSession(start_url="about:blank")
        session.start()
    try:
        page = session.acquire_page()
        page.goto(page_url, wait_until="domcontentloaded")
        payload = page.evaluate(
            _BILIBILI_WATCHLATER_PAGE_JS,
            json.dumps({"ps": BILIBILI_WATCHLATER_PS, "requestTimeoutMs": BILIBILI_PAGE_REQUEST_TIMEOUT_MS}),
            timeout=BILIBILI_BSK_EVALUATE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"B 站稍后看页内取数失败: {exc}") from exc
    finally:
        if own_session:
            session.shutdown()

    if not isinstance(payload, dict):
        raise RuntimeError(f"B 站稍后看页内 fetch 返回异常（非对象）: {payload!r}")
    if payload.get("error"):
        raise RuntimeError(f"B 站稍后看页内 fetch 失败: {payload['error']}")
    code = payload.get("code")
    if str(code) != "0":
        raise RuntimeError(f"B 站稍后看接口返回异常: code={code} message={payload.get('message')}")
    items = _extract_watchlater_items({"data": payload.get("data")})
    if items is None:
        raise RuntimeError(f"无法识别 B 站稍后看返回结构: {payload.get('data')!r}")
    # 空列表是合法结果（用户把稍后看清空）：返回 []，由 pipeline 的「0 条」WARN 承担可见性，
    # 不能抛错——否则会把真实清空的用户每轮记成一条抓取失败。
    return items


class BilibiliApiSession:
    def __init__(self, config: BilibiliConfig, timeout: int = 20) -> None:
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False  # B 站是国内站点，绕过系统代理（本地代理未开时避免 ProxyError）
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


def _parse_dynamic_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """把一条动态 item（HTTP 原样载荷或页内投影载荷）解析成统一结构；无 id 返回 None。

    HTTP 与 bsk 页内两条取数路径共用这一处解析，保证字段与去重口径完全一致（SSOT）。
    """
    dynamic_id = str(item.get("id_str") or item.get("id") or "").strip()
    if not dynamic_id:
        return None
    author = (item.get("modules") or {}).get("module_author") or {}
    published_ts = int(author.get("pub_ts") or 0)
    published_text = str(author.get("pub_time") or "").strip()
    action = str(author.get("pub_action") or "").strip()
    title = extract_bilibili_dynamic_text(item)
    link = pick_bilibili_dynamic_link(item)
    return {
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
            parsed = _parse_dynamic_item(item)
            if not parsed or parsed["id"] in seen_ids:
                continue
            seen_ids.add(parsed["id"])
            collected.append(parsed)
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

    return _dynamic_feed_result(source, uid, page_url, items, stage="api")


def _dynamic_feed_result(
    source: dict[str, Any], uid: str, page_url: str, items: list[dict[str, Any]], *, stage: str
) -> FeedFetchResult:
    """把解析后的动态条目拼成 FeedFetchResult（HTTP / bsk 两条路径共用，输出一致）。"""
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
            error=f"B站抓取失败[uid={uid}][stage={stage}][kind=empty][attempt=1][url={page_url}]: 动态接口返回成功，但未产出条目",
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


# bsk 页内取数：bsk 无法手设 Origin/Referer（forbidden header），故先导航到 space 页建立同域
# 上下文，再在页内用浏览器自带真实 cookie（credentials:include）直接 fetch 官方 API。
# 纯 GET 无需 WBI 签名与 bili_jct（已实测 code:0）。一次 evaluate 内跑完 max_pages 页翻页，
# 并在页内做字段投影——动态 item 原始载荷极大，全量经 CLI stdout 回传会爆量。
# 只回传 _parse_dynamic_item / pick_bilibili_dynamic_link / extract_bilibili_dynamic_text
# 实际读取的字段。
_BILIBILI_DYNAMIC_PAGE_JS = r"""
async (argsJson) => {
  const args = JSON.parse(argsJson);
  const base = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space";
  const pick = (obj, keys) => {
    const out = {};
    if (!obj || typeof obj !== "object") return out;
    for (const k of keys) {
      if (obj[k] !== undefined && obj[k] !== null) out[k] = obj[k];
    }
    return out;
  };
  const project = (it) => {
    const modules = it.modules || {};
    const dynamic = modules.module_dynamic || {};
    const major = dynamic.major || {};
    return {
      id_str: it.id_str,
      id: it.id,
      type: it.type,
      basic: pick(it.basic, ["jump_url"]),
      modules: {
        module_author: pick(modules.module_author, ["name", "pub_ts", "pub_time", "pub_action"]),
        module_dynamic: {
          desc: pick(dynamic.desc, ["text"]),
          additional: {desc: pick(dynamic.additional && dynamic.additional.desc, ["text"])},
          major: {
            opus: Object.assign(pick(major.opus, ["title", "jump_url"]), {
              summary: pick(major.opus && major.opus.summary, ["text"]),
            }),
            archive: pick(major.archive, ["title", "desc", "jump_url"]),
            article: pick(major.article, ["title", "desc", "jump_url"]),
            common: pick(major.common, ["title", "desc", "jump_url"]),
            music: pick(major.music, ["title", "desc", "jump_url"]),
            pgc: pick(major.pgc, ["jump_url"]),
            medialist: pick(major.medialist, ["jump_url"]),
            ugc_season: pick(major.ugc_season, ["jump_url"]),
            live: pick(major.live, ["jump_url"]),
            live_rcmd: pick(major.live_rcmd, ["jump_url"]),
          },
        },
      },
    };
  };
  const items = [];
  let offset = "";
  let code = null;
  let message = "";
  for (let i = 0; i < args.maxPages; i++) {
    const url = new URL(base);
    url.searchParams.set("host_mid", String(args.uid));
    url.searchParams.set("offset", offset);
    url.searchParams.set("timezone_offset", "-480");
    url.searchParams.set("features", "itemOpusStyle");
    let resp;
    try {
      resp = await fetch(url.toString(), {
        credentials: "include",
        signal: AbortSignal.timeout(args.requestTimeoutMs),
      });
    } catch (e) {
      return {error: "接口请求失败: " + String(e)};
    }
    let data;
    try {
      data = await resp.json();
    } catch (e) {
      return {error: "接口返回非 JSON（可能登录态失效）"};
    }
    if (i === 0) {
      code = data.code;
      message = data.message;
    }
    if (data.code !== 0) {
      return {code: data.code, message: data.message, items: items};
    }
    const pageItems = (data.data && data.data.items) || [];
    for (const it of pageItems) items.push(project(it));
    offset = (data.data && data.data.offset) || "";
    if (!(data.data && data.data.has_more) || !offset) break;
  }
  return {code: code, message: message, items: items};
}
"""


def fetch_bilibili_dynamic_feed_with_page(
    page, source: dict, limit: int = 12, timeout_ms: int = 60000
) -> FeedFetchResult:
    """bsk 页内取数版：与 fetch_bilibili_dynamic_feed 输出字段完全一致，只是取数改用真实浏览器。

    先导航到 space 页拿同域上下文/Referer，再页内 fetch 官方接口（带用户真实 cookie）。
    """
    uid = _bilibili_uid_from_source(source)
    if not uid:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source.get("feed_url", ""),
            ok=False,
            status=0,
            entries=[],
            error="暂不支持的 B 站网页源",
        )

    page_url = f"https://space.bilibili.com/{uid}/dynamic"
    eval_timeout = max(
        BILIBILI_BSK_EVALUATE_TIMEOUT_SECONDS, timeout_ms / 1000.0 + 30.0
    )
    try:
        page.goto(page_url, wait_until="domcontentloaded")
        payload = page.evaluate(
            _BILIBILI_DYNAMIC_PAGE_JS,
            json.dumps(
                {
                    "uid": uid,
                    "maxPages": 2,
                    "requestTimeoutMs": BILIBILI_PAGE_REQUEST_TIMEOUT_MS,
                }
            ),
            timeout=eval_timeout,
        )
    except Exception as exc:
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=page_url,
            ok=False,
            status=0,
            entries=[],
            error=f"B站抓取失败[uid={uid}][stage=bsk][kind=unknown][attempt=1][url={page_url}]: {exc}",
        )

    if not isinstance(payload, dict):
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=page_url,
            ok=False,
            status=0,
            entries=[],
            error=f"B站抓取失败[uid={uid}][stage=bsk][kind=payload][attempt=1][url={page_url}]: 页内 fetch 返回异常（非对象）",
        )
    if payload.get("error"):
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=page_url,
            ok=False,
            status=0,
            entries=[],
            error=f"B站抓取失败[uid={uid}][stage=bsk][kind=request][attempt=1][url={page_url}]: {payload['error']}",
        )
    code = payload.get("code")
    if str(code) != "0":
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=page_url,
            ok=False,
            status=0,
            entries=[],
            error=(
                f"B站抓取失败[uid={uid}][stage=bsk][kind=api][attempt=1][url={page_url}]: "
                f"code={code} message={payload.get('message')}"
            ),
        )

    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_item in payload.get("items") or []:
        parsed = _parse_dynamic_item(raw_item)
        if not parsed or parsed["id"] in seen_ids:
            continue
        seen_ids.add(parsed["id"])
        collected.append(parsed)
        if len(collected) >= limit:
            break

    return _dynamic_feed_result(source, uid, page_url, collected, stage="bsk")

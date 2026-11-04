from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import normalize_dedupe_title_key

# 稍后看与订阅动态同一姿势：bsk 页内 fetch 官方接口，复用日常 Chrome 登录态。
# 先导航到 watchlater 页拿同域上下文/Referer，再带 credentials 调接口。
BILIBILI_WATCHLATER_PAGE_URL = "https://www.bilibili.com/watchlater/list"
BILIBILI_WATCHLATER_API = "https://api.bilibili.com/x/v2/history/toview/web"
BILIBILI_DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
# 页内 fetch 一次跑 2 页请求：预算必须明显大于 30s「疑似登录阻塞」看门狗，否则会被误判成 BskHelpTimeout。
BILIBILI_BSK_EVALUATE_TIMEOUT_SECONDS = 120.0
# 单次 fetch 的页内超时（毫秒）：任一页卡住时快速失败，而不是拖满整个 evaluate 预算。
BILIBILI_PAGE_REQUEST_TIMEOUT_MS = 20000
# 增量翻页：连续命中已入库标题达到该条数即认为已追上历史，停止继续翻页。
BILIBILI_DUPLICATE_STOP_STREAK = 3
# 安全上限：全新库/无已知键时最多翻这么多页，避免无限翻历史。
BILIBILI_DYNAMIC_MAX_PAGES = 6
BILIBILI_STOP_END_OF_FEED = "end_of_feed"
BILIBILI_STOP_DUPLICATE_STREAK = "duplicate_streak"
BILIBILI_STOP_MAX_PAGES = "max_pages"
BILIBILI_STOP_LIMIT = "limit_reached"


def _bilibili_uid_from_source(source: dict[str, Any]) -> str:
    match = re.search(r"space\.bilibili\.com/(\d+)", str(source.get("site_url") or ""))
    return match.group(1) if match else ""


def _extract_watchlater_items(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """从稍后看接口载荷里取出条目并归一化；结构无法识别返回 None。"""
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


_BILIBILI_WATCHLATER_PAGE_JS = r"""
async (argsJson) => {
  const args = JSON.parse(argsJson);
  let resp;
  try {
    resp = await fetch(args.url, {
      credentials: "include",
      signal: AbortSignal.timeout(args.requestTimeoutMs),
    });
  } catch (e) {
    return { error: "接口请求失败: " + String(e) };
  }
  try {
    return await resp.json();
  } catch (e) {
    return { error: "接口返回非 JSON（可能登录态失效）" };
  }
}
"""


def fetch_bilibili_watchlater_with_page(
    page, *, ps: int = 20, timeout_ms: int = 60000
) -> list[dict[str, Any]]:
    """bsk 页内取数版：先导航到稍后看页拿同域上下文，再页内 fetch 官方接口。"""
    eval_timeout = max(BILIBILI_BSK_EVALUATE_TIMEOUT_SECONDS, timeout_ms / 1000.0 + 30.0)
    page.goto(BILIBILI_WATCHLATER_PAGE_URL, wait_until="domcontentloaded")
    payload = page.evaluate(
        _BILIBILI_WATCHLATER_PAGE_JS,
        json.dumps(
            {
                "url": f"{BILIBILI_WATCHLATER_API}?ps={ps}",
                "requestTimeoutMs": BILIBILI_PAGE_REQUEST_TIMEOUT_MS,
            }
        ),
        timeout=eval_timeout,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("B站稍后看页内 fetch 返回异常（非对象）")
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    code = payload.get("code")
    if str(code) != "0":
        message = str(payload.get("message") or "")
        if str(code) == "-101":
            raise RuntimeError("B站登录态失效（-101），请在日常 Chrome 登录 B站后重试")
        raise RuntimeError(f"B站稍后看接口异常: code={code} message={message}")
    items = _extract_watchlater_items(payload)
    if items is None:
        raise RuntimeError("无法识别 B站稍后看返回结构")
    # 空列表是合法结果（用户把稍后看清空）：返回 []，由 pipeline 的「0 条」WARN
    # 承担可见性，不能抛错——否则会把真实清空的用户每轮记成一条抓取失败。
    return items


def fetch_bilibili_watchlater(
    env_path: str | Path | None = None, *, session=None
) -> list[dict[str, Any]]:
    """抓取 B 站「稍后看」：bsk 页内 fetch，复用日常 Chrome 登录态。

    env_path 仅为兼容 laterhub 的 `fetcher(ENV_PATH, session=...)` 调用姿势，已不使用。
    session 为空时自建一个 bsk 会话（CLI/单测入口）。
    """
    if session is not None:
        return fetch_bilibili_watchlater_with_page(session.acquire_page())
    from connectors._shared.bsk_session import BskRunnerSession

    with BskRunnerSession(start_url="about:blank") as own_session:
        return fetch_bilibili_watchlater_with_page(own_session.acquire_page())


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


def _dynamic_feed_result(
    source: dict[str, Any],
    scope_label: str,
    page_url: str,
    items: list[dict[str, Any]],
    *,
    stage: str,
    stop_reason: str = "",
    item_author_as_name: bool = False,
) -> FeedFetchResult:
    """把解析后的动态条目拼成 FeedFetchResult（bsk 页内取数路径）。

    item_author_as_name: 聚合关注流一个源里混着多个作者，条目级 source_name 取作者名
    （缺失回落源名）；逐 UP 源仍用源名。
    """
    entries: list[FeedEntry] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        link = str(item.get("link") or item.get("dynamic_link") or page_url).strip()
        summary = str(item.get("summary") or "").strip()
        published = str(item.get("published_at") or item.get("published_text") or "").strip()
        item_source_name = source["name"]
        if item_author_as_name:
            item_source_name = str(item.get("author") or "").strip() or source["name"]
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=item_source_name,
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
            error=f"B站抓取失败[{scope_label}][stage={stage}][kind=empty][attempt=1][url={page_url}]: 动态接口返回成功，但未产出条目",
        )

    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
        stop_reason=stop_reason,
    )


# bsk 页内取数：bsk 无法手设 Origin/Referer（forbidden header），故先导航到同域页（space 源页 /
# t.bilibili.com 关注页）建立上下文，再在页内用浏览器自带真实 cookie（credentials:include）
# 直接 fetch 官方 API。apiPath/apiType 决定 feed/space（逐 UP）或 feed/all（关注流）。
# 纯 GET 无需 WBI 签名与 bili_jct（已实测 code:0）。每次 evaluate 只取一页，翻页与停止判定在
# Python 侧做（按已入库标题增量翻页）；页内只做字段投影——动态 item 原始载荷极大，全量经
# CLI stdout 回传会爆量。只回传 _parse_dynamic_item / pick_bilibili_dynamic_link /
# extract_bilibili_dynamic_text 实际读取的字段。
_BILIBILI_DYNAMIC_PAGE_JS = r"""
async (argsJson) => {
  const args = JSON.parse(argsJson);
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
  const url = new URL("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/" + args.apiPath);
  // 逐 UP 源（space）按 host_mid 取数；聚合关注流（all）按“我关注的全部人”取数，不传 host_mid。
  if (args.uid) {
    url.searchParams.set("host_mid", String(args.uid));
  }
  if (args.apiType) {
    url.searchParams.set("type", args.apiType);
  }
  url.searchParams.set("offset", args.offset || "");
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
  if (data.code !== 0) {
    return {code: data.code, message: data.message, items: []};
  }
  const container = data.data || {};
  return {
    code: data.code,
    message: data.message,
    items: (container.items || []).map(project),
    offset: container.offset || "",
    has_more: !!container.has_more,
  };
}
"""


BILIBILI_FOLLOWING_PAGE_URL = "https://t.bilibili.com/"
BILIBILI_FOLLOWING_API_PATH = "all"
BILIBILI_FOLLOWING_API_TYPE = "all"


def _scope_label(uid: str, error_scope: str) -> str:
    """错误前缀的定位标识：逐 UP 源沿用 uid=...，聚合源用 error_scope。"""
    return f"uid={uid}" if uid else error_scope


def _dynamic_error(
    source: dict[str, Any], scope_label: str, page_url: str, *, kind: str, detail: str
) -> FeedFetchResult:
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=page_url,
        ok=False,
        status=0,
        entries=[],
        error=f"B站抓取失败[{scope_label}][stage=bsk][kind={kind}][attempt=1][url={page_url}]: {detail}",
    )


def _fetch_bilibili_feed_with_page(
    page,
    source: dict,
    *,
    uid: str,
    page_url: str,
    api_path: str,
    api_type: str,
    error_scope: str,
    aggregate: bool = False,
    context_url: str = "",
    limit: int = 12,
    timeout_ms: int = 60000,
) -> FeedFetchResult:
    """bsk 页内取数（逐 UP 的 feed/space 与关注流的 feed/all 共用这一处驱动）。

    uid 为空即聚合关注流：不传 host_mid，错误前缀用 error_scope。
    context_url：实际导航的落脚页，默认 page_url。页内 fetch 只要同站 Origin + 登录 cookie，
    所以关注流改用稳定页（见 fetch_bilibili_following_feed_with_page）。
    """
    scope_label = _scope_label(uid, error_scope)
    eval_timeout = max(
        BILIBILI_BSK_EVALUATE_TIMEOUT_SECONDS, timeout_ms / 1000.0 + 30.0
    )
    try:
        page.goto(context_url or page_url, wait_until="domcontentloaded")
    except Exception as exc:
        return _dynamic_error(source, scope_label, page_url, kind="unknown", detail=str(exc))

    # 已知键约定已是归一化标题键（应用层用 normalize_dedupe_title_key 生成），此处不再二次归一化。
    known = set(source.get("known_entry_keys") or ())
    offset = ""
    streak = 0
    seen_ids: set[str] = set()
    collected: list[dict[str, Any]] = []
    pages = 0
    stop_reason = ""

    while pages < BILIBILI_DYNAMIC_MAX_PAGES:
        pages += 1
        try:
            payload = page.evaluate(
                _BILIBILI_DYNAMIC_PAGE_JS,
                json.dumps(
                    {
                        "uid": uid,
                        "apiPath": api_path,
                        "apiType": api_type,
                        "offset": offset,
                        "requestTimeoutMs": BILIBILI_PAGE_REQUEST_TIMEOUT_MS,
                    }
                ),
                timeout=eval_timeout,
            )
        except Exception as exc:
            return _dynamic_error(source, scope_label, page_url, kind="unknown", detail=str(exc))

        if not isinstance(payload, dict):
            return _dynamic_error(
                source,
                scope_label,
                page_url,
                kind="payload",
                detail="页内 fetch 返回异常（非对象）",
            )
        if payload.get("error"):
            return _dynamic_error(
                source, scope_label, page_url, kind="request", detail=str(payload["error"])
            )
        code = payload.get("code")
        if str(code) != "0":
            detail = f"code={code} message={payload.get('message')}"
            if str(code) == "-101":
                detail += " B站登录态失效（-101），请在日常 Chrome 登录 B站后重试"
            return _dynamic_error(source, scope_label, page_url, kind="api", detail=detail)

        for raw_item in payload.get("items") or []:
            parsed = _parse_dynamic_item(raw_item)
            if not parsed:
                continue
            if parsed["id"] in seen_ids:
                # 置顶等重复 id：不算新条目，也不打断连续命中计数。
                continue
            title = str(parsed.get("title") or "").strip()
            if not title:
                continue
            seen_ids.add(parsed["id"])
            if len(collected) < limit:
                collected.append(parsed)
            if normalize_dedupe_title_key(title) in known:
                streak += 1
            else:
                streak = 0

        if streak >= BILIBILI_DUPLICATE_STOP_STREAK:
            stop_reason = BILIBILI_STOP_DUPLICATE_STREAK
            break
        if len(collected) >= limit:
            stop_reason = BILIBILI_STOP_LIMIT
            break
        has_more = bool(payload.get("has_more"))
        next_offset = str(payload.get("offset") or "")
        if not has_more or not next_offset:
            stop_reason = BILIBILI_STOP_END_OF_FEED
            break
        offset = next_offset
    else:
        stop_reason = BILIBILI_STOP_MAX_PAGES

    return _dynamic_feed_result(
        source,
        scope_label,
        page_url,
        collected,
        stage="bsk",
        stop_reason=stop_reason,
        item_author_as_name=aggregate,
    )


def fetch_bilibili_dynamic_feed_with_page(
    page, source: dict, limit: int = 12, timeout_ms: int = 60000
) -> FeedFetchResult:
    """逐 UP 源：先导航到该源 space 页拿同域上下文/Referer，再页内 fetch feed/space。"""
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
    return _fetch_bilibili_feed_with_page(
        page,
        source,
        uid=uid,
        page_url=f"https://space.bilibili.com/{uid}/dynamic",
        api_path="space",
        api_type="",
        error_scope="uid",
        limit=limit,
        timeout_ms=timeout_ms,
    )


def fetch_bilibili_following_feed_with_page(
    page, source: dict, limit: int = 12, timeout_ms: int = 60000
) -> FeedFetchResult:
    """聚合关注流：在稳定页落脚，页内 fetch feed/all（不传 host_mid）。

    不在 t.bilibili.com 上落脚：那是自更新页（计时器/轮播/推荐卡片持续变化），而
    evaluate 属无目标动作，bsk 对它就保持整页基线校验，sync 后页面一变即
    `permission_denied: The action's observed content changed`；实测该页反复拒（3 连拒
    后整源失败），稳定页 0/5 次拒。页内 fetch 只需同站 Origin + 登录 cookie，故复用稍后看
    的稳定页（同一约定见 video.py 的 BILIBILI_STABLE_PAGE_URL）。
    """
    return _fetch_bilibili_feed_with_page(
        page,
        source,
        uid="",
        page_url=BILIBILI_FOLLOWING_PAGE_URL,
        context_url=BILIBILI_WATCHLATER_PAGE_URL,
        api_path=BILIBILI_FOLLOWING_API_PATH,
        api_type=BILIBILI_FOLLOWING_API_TYPE,
        error_scope="following",
        aggregate=True,
        limit=limit,
        timeout_ms=timeout_ms,
    )

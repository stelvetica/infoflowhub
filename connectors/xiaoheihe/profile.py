from __future__ import annotations

import json
import re
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import (
    clean_line,
    now_text,
    parse_published_datetime,
    resolve_web_target,
    result_error,
)

# 用户主页动态（帖子）列表 API：GET /bbs/app/profile/events?list_type=moment&userid=<id>
PROFILE_EVENTS_PATH = "/bbs/app/profile/events"
# 小黑盒用户主页帖子必须登录态才可加载（未登录 API 返回空 moments / 跳登录）。
XIAOHEIHE_LOGIN_HINT = "请运行 login_profiles.py xiaoheihe 扫码登录小黑盒（登录态存入共享 auth profile）。"


def _normalize_xiaoheihe_link(linkid: str) -> str:
    linkid = (linkid or "").strip()
    if not linkid:
        return ""
    return f"https://www.xiaoheihe.cn/app/bbs/link/{linkid}"


def _extract_profile_moments_from_network(
    page, target_url: str, timeout_ms: int, limit: int
) -> tuple[list[dict], str]:
    moments: list[dict] = []
    response_error = ""

    def on_response(response):
        nonlocal response_error
        try:
            url = response.url
            if PROFILE_EVENTS_PATH not in url:
                return
            payload = response.json()
            result = payload.get("result") or {}
            for item in (result.get("moments") or []):
                if isinstance(item, dict):
                    linkid = str(item.get("linkid") or item.get("link_id") or "").strip()
                    if linkid:
                        moments.append(item)
                    if len(moments) >= limit * 2:
                        return
        except Exception as exc:
            response_error = str(exc)

    page.on("response", on_response)
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(5000)
    finally:
        page.remove_listener("response", on_response)
    return moments, response_error


# bsk 后端无 page.on("response")（`bsk network` 只回 URL/错误文本，不给 body），
# 改用「页内借 $hbRequest 拿签名 → 手动 fetch 取数据」：与收藏夹路径同一套路。
# $hbRequest 会吞掉 query 参数（实测 /bbs/app/profile/events?userid=... 仍报 "userid 缺失"），
# 故先包一层 fetch/XHR 捕获它实际发出的签名 URL，再补上 userid/list_type 自行请求。
_PROFILE_EVENTS_JS = r"""
async (argsJson) => {
    const args = JSON.parse(argsJson);
    const withTimeout = (promise, ms) => Promise.race([
        promise,
        new Promise((_, reject) => setTimeout(() => reject(new Error("hbRequest timeout")), ms)),
    ]);
    let captured = "";
    const origFetch = window.fetch;
    const origOpen = XMLHttpRequest.prototype.open;
    window.fetch = function (...a) {
        try {
            const u = typeof a[0] === "string" ? a[0] : (a[0] && a[0].url);
            if (!captured && u && u.indexOf(args.apiPath) >= 0) captured = u;
        } catch (e) {}
        return origFetch.apply(this, a);
    };
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
        try { if (!captured && url && url.indexOf(args.apiPath) >= 0) captured = url; } catch (e) {}
        return origOpen.call(this, method, url, ...rest);
    };
    try {
        const app = document.querySelector("#app");
        const req = app && app.__vue_app__ && app.__vue_app__.config.globalProperties.$hbRequest;
        if (!req) return {error: "页面未就绪（$hbRequest 不可用），可能未登录或未加载完成"};
        try {
            await withTimeout(req(args.apiPath, {}, {method: "GET"}), 15000);
        } catch (e) {
            // 首请求只为拿签名，失败也继续（缺 userid 时会返回 failed，属正常）。
        }
    } finally {
        window.fetch = origFetch;
        XMLHttpRequest.prototype.open = origOpen;
    }
    if (!captured) return {error: "未能获取小黑盒接口签名，前端封装可能已变更"};
    const url = new URL(captured);
    url.searchParams.set("list_type", args.listType);
    url.searchParams.set("userid", String(args.userid));
    if (args.offset !== undefined) url.searchParams.set("offset", String(args.offset));
    if (args.limit !== undefined) url.searchParams.set("limit", String(args.limit));
    let resp;
    try {
        resp = await fetch(url.toString(), {credentials: "include", signal: AbortSignal.timeout(15000)});
    } catch (e) {
        return {error: "接口请求失败: " + String(e)};
    }
    let data;
    try {
        data = await resp.json();
    } catch (e) {
        return {error: "接口返回非 JSON（可能登录态失效）"};
    }
    const moments = (data.result && data.result.moments) || [];
    return {
        status: data.status,
        msg: data.msg,
        moments: moments.map((m) => ({
            linkid: m.linkid || m.link_id || "",
            title: m.title || "",
            description: m.description || "",
            create_at: m.create_at,
        })),
    };
}
"""


def _extract_profile_moments_via_hbrequest(
    page, target_url: str, userid: str, timeout_ms: int, limit: int
) -> tuple[list[dict], str]:
    page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(5000)
    payload = page.evaluate(
        _PROFILE_EVENTS_JS,
        json.dumps(
            {
                "apiPath": PROFILE_EVENTS_PATH,
                "listType": "moment",
                "userid": str(userid),
                "offset": 0,
                "limit": limit,
            }
        ),
    )
    if not isinstance(payload, dict):
        return [], "小黑盒接口返回异常（非对象）"
    if payload.get("error"):
        return [], str(payload["error"])
    moments = [m for m in (payload.get("moments") or []) if isinstance(m, dict)]
    return moments, ""


def _extract_profile_moments(
    page, target_url: str, userid: str, timeout_ms: int, limit: int
) -> tuple[list[dict], str]:
    """按后端能力选路：copy（Playwright page 有 on）走响应拦截；bsk 走 $hbRequest 直取。"""
    if getattr(page, "on", None) is not None:
        return _extract_profile_moments_from_network(page, target_url, timeout_ms, limit)
    return _extract_profile_moments_via_hbrequest(page, target_url, userid, timeout_ms, limit)


def _normalize_moment_to_entry(source: dict, item: dict) -> FeedEntry | None:
    linkid = str(item.get("linkid") or item.get("link_id") or "").strip()
    link = _normalize_xiaoheihe_link(linkid)
    if not link:
        return None
    raw_title = clean_line(str(item.get("title") or ""))
    if not raw_title:
        return None
    title = re.sub(r"\s+", " ", raw_title).strip()
    summary = clean_line(str(item.get("description") or "")) or title

    published = ""
    create_at = item.get("create_at") or item.get("createAt")
    if create_at is not None:
        try:
            ts = int(create_at)
            if ts > 10**12:
                ts //= 1000
            published = datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M")
        except Exception:
            published = ""

    return FeedEntry(
        source_id=source["id"],
        source_name=source["name"],
        title=title,
        link=link,
        published=published or now_text(),
        summary=summary,
    )


def fetch_xiaoheihe_profile_with_page(page, source: dict, timeout_ms: int = 60000, limit: int = 12) -> FeedFetchResult:
    limit = max(1, min(limit, 12))
    target = resolve_web_target(source)
    if not target or target.site != "xiaoheihe":
        return result_error(source, "暂不支持的小黑盒订阅源")

    try:
        moments, response_error = _extract_profile_moments(
            page, target.page_url, target.uid, timeout_ms, limit
        )
    except PlaywrightTimeoutError as exc:
        return result_error(source, f"小黑盒订阅抓取超时: {exc}")
    except Exception as exc:
        return result_error(source, f"小黑盒订阅抓取失败: {exc}")

    if response_error and not moments:
        return result_error(source, f"小黑盒订阅抓取失败: {response_error}")

    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    for item in moments:
        entry = _normalize_moment_to_entry(source, item)
        if not entry or entry.link in seen_links:
            continue
        seen_links.add(entry.link)
        entries.append(entry)
        if len(entries) >= limit:
            break

    if not entries:
        return result_error(source, f"小黑盒主页可访问，但未解析到帖子内容（可能未登录）。{XIAOHEIHE_LOGIN_HINT}")

    entries.sort(key=lambda item: parse_published_datetime(item.published) or datetime.min, reverse=True)
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )
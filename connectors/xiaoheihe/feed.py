from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from apps.laterhub.config import DEBUG_DIR
from connectors._shared.bsk_session import BskRunnerSession
from connectors._shared.common import FetchBatch


XIAOHEIHE_HOME_URL = "https://www.xiaoheihe.cn/"
MESSAGE_PREFIX = "[小黑盒]"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_JSON_PATH = DEBUG_DIR / "tmp_pw_xiaoheihe_favorites.json"

# 收藏夹 API（PC 网页版收藏夹页面已下线，但此 API 仍可用）。
# 经抓包确认：GET /bbs/app/favour/list，翻页用 offset+limit，total_page 表总页数。
FAVOUR_API_PATH = "/bbs/app/favour/list"
FAVOUR_PAGE_SIZE = 20
FAVOUR_MAX_PAGES = 50  # 安全上限，防异常死循环
# 日常短抓取：最多翻这么多页；每 30 天一次的深扫才跑满上限。
FAVOUR_SHORT_MAX_PAGES = 3
# 日常短抓取：按收藏时间倒序，连续命中这么多条已入库条目即认为已追平历史。
FAVOUR_KNOWN_STREAK = 3


def _log(msg: str) -> None:
    print(f"{MESSAGE_PREFIX} {msg}", flush=True)


def normalize_xiaoheihe_item(item: dict[str, Any]) -> dict[str, Any] | None:
    # API 返回字段为 linkid（非 link_id），无 url 需自拼
    link_id = str(item.get("linkid") or item.get("link_id") or "").strip()
    raw_title = (item.get("title") or "").strip()

    if not link_id:
        return None

    url = f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}"
    title = re.sub(r"\s+", " ", raw_title).strip()
    if not title:
        title = f"小黑盒帖子_{link_id[:8]}"
    if title.startswith("帖子_"):
        return None

    return {
        "url": url,
        "title": title,
        "source": "xiaoheihe_favor",
        "owner": "",
        "tags": None,
        "raw": item,
    }


# bsk 后端没有请求拦截能力（无法截获网络请求），改用页内取数：
# 借前端 $hbRequest 拿合法签名（它会自动算 hkey/nonce/_time 等），同时包一层
# fetch/XHR 捕获它实际发出的签名 URL，再手动补 offset/limit 自行翻页。
# 与 connectors/xiaoheihe/profile.py 的 $hbRequest 直取路径同一套路。
#
# ponytail: 依赖前端 $hbRequest / #app.__vue_app__ 的内部结构，前端封装变更时会失败——
# 失败会以「未能获取小黑盒收藏接口签名」文案抛出（可见、不静默），届时按新结构更新本 JS 即可。
_FAVOUR_LIST_JS = r"""
async (argsJson) => {
    const args = JSON.parse(argsJson);
    const withTimeout = (promise, ms) => Promise.race([
        promise,
        new Promise((_, reject) => setTimeout(() => reject(new Error("hbRequest timeout")), ms)),
    ]);
    // 捕获 $hbRequest 实际发出的签名 URL（它吞掉 query 参数，故不能自己拼签名）。
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
            // 首请求只为拿签名，失败也继续（此处不带 offset，返回什么不重要）。
            await withTimeout(req(args.apiPath, {}, {method: "GET"}), 15000);
        } catch (e) {}
    } finally {
        window.fetch = origFetch;
        XMLHttpRequest.prototype.open = origOpen;
    }
    if (!captured) return {error: "未能获取小黑盒收藏接口签名（可能未登录或前端封装已变更）"};
    // captured 已含合法签名参数，在其基础上补 limit/offset 翻页。
    // 带 base：前端若发相对路径，裸 new URL 会 TypeError 且报错不可读。
    const base = new URL(captured, location.origin);
    const all = [];
    const known = new Set(args.knownIds || []);
    let knownStreak = 0;
    let stoppedByKnown = false;
    let totalPage = 1;
    let totalPageKnown = false;
    let pagesFetched = 0;
    let complete = false;
    for (let i = 0; i < args.maxPages; i++) {
        const u = new URL(base.toString());
        u.searchParams.set("limit", String(args.pageSize));
        u.searchParams.set("offset", String(i * args.pageSize));
        let resp;
        try {
            resp = await fetch(u.toString(), {credentials: "include", signal: AbortSignal.timeout(15000)});
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
            if (data.status !== "ok") {
                return {error: "api status=" + data.status + " msg=" + (data.msg || "")};
            }
            totalPageKnown = data.total_page !== undefined && data.total_page !== null;
            totalPage = data.total_page || 1;
        } else if (data.status && data.status !== "ok") {
            return {error: "api status=" + data.status + " msg=" + (data.msg || "")};
        }
        pagesFetched++;
        const items = data.result || [];
        if (items.length === 0) { complete = true; break; }
        for (const it of items) {
            all.push({linkid: it.linkid, title: it.title || "", is_favour: it.is_favor || it.is_favour});
            // 日常短抓取：连续命中已知 linkid 即认为已追平历史。
            if (!args.deep) {
                const id = String(it.linkid || "");
                knownStreak = id && known.has(id) ? knownStreak + 1 : 0;
                if (knownStreak >= args.knownStreak) stoppedByKnown = true;
            }
        }
        if (stoppedByKnown) { complete = false; break; }
        if (totalPageKnown) {
            if (i + 1 >= totalPage) { complete = true; break; }
        } else if (items.length < args.pageSize) {
            // total_page 缺失时以「本页未满」判断到底，否则继续翻页（上限 maxPages）。
            complete = true; break;
        }
    }
    // 未走完（撞 maxPages 上限，或 total_page 缺失且本页仍满）= 只拿到部分数据，回传标志供 Python 侧 WARN。
    return {
        total_page: totalPage,
        total_page_known: totalPageKnown,
        pages_fetched: pagesFetched,
        truncated: !complete,
        count: all.length,
        items: all,
    };
}
"""


def _known_link_ids(known_urls: list[str] | None) -> list[str]:
    """从已入库 URL 提取 linkid，供页内做「已追平历史」判定。"""
    found: list[str] = []
    for url in known_urls or []:
        match = re.search(r"/link/(\d+)", str(url))
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def _fetch_favour_via_api(
    page, *, deep: bool = True, known_urls: list[str] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """页内借 $hbRequest 拿签名 → 手动 fetch 翻页拉收藏。

    $hbRequest 自动算 hkey 签名但吞没 query 参数（page/offset 均不进 URL），
    故先包一层 fetch/XHR 捕获它实际发出的签名 URL，再手动 fetch 带 offset 翻页。
    翻页靠 offset（page 无效）。签名与翻页全在页内一次 evaluate 内完成，不再依赖网络拦截。
    deep=False 为日常短抓取：连续命中已知条目即收工，最多翻短页数。
    """
    payload = page.evaluate(
        _FAVOUR_LIST_JS,
        json.dumps(
            {
                "apiPath": FAVOUR_API_PATH,
                "pageSize": FAVOUR_PAGE_SIZE,
                "maxPages": FAVOUR_MAX_PAGES if deep else FAVOUR_SHORT_MAX_PAGES,
                "deep": bool(deep),
                "knownIds": _known_link_ids(known_urls),
                "knownStreak": FAVOUR_KNOWN_STREAK,
            }
        ),
    )
    if not isinstance(payload, dict):
        raise RuntimeError("小黑盒收藏接口返回异常（非对象）")
    if payload.get("error"):
        raise RuntimeError(f"收藏 API 调用失败: {payload['error']}")
    items = payload.get("items") or []
    _log(f"API 翻页完成，total_page={payload.get('total_page')} 获取 {len(items)} 条")
    truncated = bool(payload.get("truncated"))
    if truncated:
        _log(
            f"[WARN] 收藏未取全：已抓 {payload.get('pages_fetched')} 页"
            f"（total_page={payload.get('total_page')}，上限 {FAVOUR_MAX_PAGES}），仅返回部分数据"
        )
    return items, truncated


def fetch_xiaoheihe_favorites(
    env_path: str | Path | None = None,
    *,
    session: BskRunnerSession | None = None,
    deep: bool = False,
    known_urls: list[str] | None = None,
) -> FetchBatch:
    own_session = session is None
    if own_session:
        session = BskRunnerSession(start_url="about:blank")
        session.start()

    items: list[dict[str, Any]] = []
    truncated = True
    try:
        page = session.acquire_page()

        # 导航到首页激活登录态 cookie（收藏夹页面已下线，直接走 API）
        try:
            page.goto(XIAOHEIHE_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            raise RuntimeError(f"导航小黑盒首页失败: {exc}") from exc
        page.wait_for_timeout(4000)

        # 校验登录态：restore_login 必须返回 ok
        login_ok = page.evaluate(r"""
        async () => {
            try {
                const withTimeout = (promise, ms) => Promise.race([
                    promise,
                    new Promise((_, reject) => setTimeout(() => reject(new Error("restore_login timeout")), ms)),
                ]);
                const app = document.querySelector('#app');
                const req = app.__vue_app__.config.globalProperties.$hbRequest;
                const resp = await withTimeout(req("/account/restore_login", {}, {method: "GET"}), 15000);
                const data = resp.data || resp;
                return data.status === "ok";
            } catch(e) { return false; }
        }
        """)
        if not login_ok:
            raise RuntimeError("小黑盒登录态失效（restore_login 未返回 ok），请在日常 Chrome 中重新登录小黑盒")

        items, truncated = _fetch_favour_via_api(page, deep=deep, known_urls=known_urls)

        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_JSON_PATH.write_text(
            json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"调试数据已写入 {DEBUG_JSON_PATH}")
        try:
            page.close()
        except Exception:
            pass
    finally:
        if own_session:
            session.shutdown()

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        normalized = normalize_xiaoheihe_item(item)
        if not normalized:
            continue
        url = normalized["url"]
        if url in seen:
            continue
        seen.add(url)
        result.append(normalized)
    return FetchBatch(items=result, complete=bool(deep and not truncated))

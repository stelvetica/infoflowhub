from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from apps.laterhub.config import DEBUG_DIR
from connectors._shared.chrome_runner import CopyRunnerSession
from connectors._shared.common import USER_AGENT


XIAOHEIHE_HOME_URL = "https://www.xiaoheihe.cn/"
LATERHUB_RUNNER_DEBUG_PORT = 9225
MESSAGE_PREFIX = "[小黑盒]"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_JSON_PATH = DEBUG_DIR / "tmp_pw_xiaoheihe_favorites.json"

# 收藏夹 API（PC 网页版收藏夹页面已下线，但此 API 仍可用）。
# 经抓包确认：GET /bbs/app/favour/list，翻页用 offset+limit，total_page 表总页数。
FAVOUR_API_PATH = "/bbs/app/favour/list"
FAVOUR_PAGE_SIZE = 20
FAVOUR_MAX_PAGES = 50  # 安全上限，防异常死循环


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


def _fetch_favour_via_api(page) -> list[dict[str, Any]]:
    """用小黑盒前端 $hbRequest 拿到合法签名，再手动 fetch 翻页拉全量收藏。

    $hbRequest 自动算 hkey 签名但吞没 query 参数（page/offset 均不进 URL），
    故先用它发首请求拿 hkey/nonce/_time/heybox_id，再手动 fetch 带 offset 翻页。
    hkey 可在一次会话内复用（已抓包验证）。翻页靠 offset（page 无效）。
    """
    # Step 1: $hbRequest 发首请求，拦截实际 URL 提取签名参数
    sign_params: dict[str, str] = {}

    def _capture(request):
        if "favour/list" in request.url and not sign_params:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(request.url).query)
            sign_params.update({k: v[0] for k, v in qs.items()})

    page.on("request", _capture)
    page.evaluate(r"""
        async (apiPath) => {
            const withTimeout = (promise, ms) => Promise.race([
                promise,
                new Promise((_, reject) => setTimeout(() => reject(new Error("hbRequest timeout")), ms)),
            ]);
            const app = document.querySelector('#app');
            const req = app.__vue_app__.config.globalProperties.$hbRequest;
            const resp = await withTimeout(req(apiPath, {}, {method: "GET"}), 15000);
            return (resp.data || resp).status;
        }
    """, FAVOUR_API_PATH)
    page.wait_for_timeout(1500)
    if not sign_params:
        raise RuntimeError("未能从 $hbRequest 拦截到签名参数，前端封装可能已变更")
    page.remove_listener("request", _capture)

    # Step 2: 手动 fetch 带 offset 翻页
    js = r"""
        async (argsJson) => {
            const args = JSON.parse(argsJson);
            const base = "https://api.xiaoheihe.cn" + args.apiPath;
            const all = [];
            let totalPage = 1;
            for (let i = 0; i < args.maxPages; i++) {
                const u = new URL(base);
                for (const [k, v] of Object.entries(args.sign)) u.searchParams.set(k, v);
                u.searchParams.set("limit", String(args.pageSize));
                u.searchParams.set("offset", String(i * args.pageSize));
                const resp = await fetch(u.toString(), {
                    credentials: "include",
                    signal: AbortSignal.timeout(15000),
                });
                const data = await resp.json();
                if (i === 0) {
                    if (data.status !== "ok") {
                        return {error: "api status=" + data.status + " msg=" + (data.msg || "")};
                    }
                    totalPage = data.total_page || 1;
                }
                const items = data.result || [];
                if (items.length === 0) break;
                for (const it of items) {
                    all.push({linkid: it.linkid, title: it.title || "", is_favour: it.is_favor || it.is_favour});
                }
                if (i + 1 >= totalPage) break;
            }
            return {total_page: totalPage, count: all.length, items: all};
        }
    """
    import json as _json
    result = page.evaluate(js, _json.dumps({
        "apiPath": FAVOUR_API_PATH,
        "sign": sign_params,
        "pageSize": FAVOUR_PAGE_SIZE,
        "maxPages": FAVOUR_MAX_PAGES,
    }))
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"收藏 API 调用失败: {result['error']}")
    items = result.get("items", []) if isinstance(result, dict) else []
    _log(f"API 翻页完成，total_page={result.get('total_page')} 获取 {len(items)} 条")
    return items


def fetch_xiaoheihe_favorites(
    env_path: str | Path | None = None,
    session: CopyRunnerSession | None = None,
) -> list[dict[str, Any]]:
    own_session = session is None
    if own_session:
        from connectors.auth.providers.browser_profiles import LATERHUB_RUNNER_DIR, _default_chrome_profile_dir
        session = CopyRunnerSession(
            runner_dir=LATERHUB_RUNNER_DIR,
            debug_port=LATERHUB_RUNNER_DEBUG_PORT,
            source_profile_dir=_default_chrome_profile_dir(),
            start_url="about:blank",
            headless=True,
            extra_args=[f"--user-agent={USER_AGENT}", "--lang=zh-CN,zh;q=0.9,en;q=0.8"],
        )
        session.start()

    items: list[dict[str, Any]] = []
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
            raise RuntimeError("小黑盒登录态失效（restore_login 未返回 ok），请运行 login_profiles.py xiaoheihe 重新登录")

        items = _fetch_favour_via_api(page)

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
    return result

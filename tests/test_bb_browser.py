"""bb-browser 登录态导出自检（最小 assert，无框架）。

前置：本机已全局安装 bb-browser、已关 ABE、Chrome 已登录 B站并运行。
运行：python tests/test_bb_browser.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from connectors.auth.providers.bb_browser import (  # noqa: E402
    export_cookies_from_live_chrome,
    get_cdp_endpoint,
    validate_daemon,
)
from connectors.auth.providers.bilibili import (  # noqa: E402
    export_bilibili_cookie_from_live_chrome,
    get_bilibili_cookie,
)
from connectors._shared.chrome_runner import LiveChromeSession  # noqa: E402


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def test_cookie_export() -> None:
    print("[1] validate_daemon ...")
    status = validate_daemon()
    print("    ->", status.get("hint"))
    if not status.get("is_available"):
        _fail(f"daemon 不可用: {status.get('hint')}")

    print("[2] get_cdp_endpoint ...")
    endpoint = get_cdp_endpoint()
    print("    ->", endpoint)
    if not endpoint.startswith("http://"):
        _fail(f"CDP 端点非法: {endpoint}")

    print("[3] export B站 cookies (raw) ...")
    cookies = export_cookies_from_live_chrome(
        (".bilibili.com",),
        open_url="https://www.bilibili.com/",
    )
    names = {str(c.get("name") or "") for c in cookies}
    print(f"    共 {len(cookies)} 个 B站 cookie")
    missing = [k for k in ("SESSDATA", "bili_jct", "DedeUserID") if k not in names]
    if missing:
        _fail(f"未读到 B站关键登录态 cookie，缺失: {missing}")

    print("[4] export_bilibili_cookie_from_live_chrome (端到端) ...")
    cookie = export_bilibili_cookie_from_live_chrome()
    print("    已持久化，len(cookie) =", len(cookie))
    if not get_bilibili_cookie():
        _fail("持久化后读不到 B站 cookie")
    if not cookie.startswith("SESSDATA="):
        _fail("拼出的 cookie 串不以 SESSDATA= 开头")


def test_live_chrome_session() -> None:
    print("[5] LiveChromeSession 直连真实 Chrome + 导航 ...")
    with LiveChromeSession() as session:
        page = session.acquire_page()
        page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        title = page.title()
        print(f"    打开 B站，标题: {title[:40]}")
        if "bilibili" not in title.lower():
            _fail(f"LiveChromeSession 未能正常导航/加载 B站: {title}")
        try:
            page.close()
        except Exception:
            pass
    print("PASS: bb-browser 从真实 Chrome 导出登录态 + LiveChromeSession 全链路通过")


def main() -> None:
    test_cookie_export()
    test_live_chrome_session()


if __name__ == "__main__":
    main()

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
    validate_daemon,
)
from connectors.auth.providers.bilibili import (  # noqa: E402
    export_bilibili_cookie_from_live_chrome,
    get_bilibili_cookie,
)


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    print("[1] validate_daemon ...")
    status = validate_daemon()
    print("    ->", status.get("hint"))
    if not status.get("is_available"):
        _fail(f"daemon 不可用: {status.get('hint')}")

    print("[2] export B站 cookies (raw) ...")
    cookies = export_cookies_from_live_chrome(
        (".bilibili.com",),
        open_url="https://www.bilibili.com/",
    )
    names = {str(c.get("name") or "") for c in cookies}
    print(f"    共 {len(cookies)} 个 B站 cookie")
    missing = [k for k in ("SESSDATA", "bili_jct", "DedeUserID") if k not in names]
    if missing:
        _fail(f"未读到 B站关键登录态 cookie，缺失: {missing}")

    print("[3] export_bilibili_cookie_from_live_chrome (端到端) ...")
    cookie = export_bilibili_cookie_from_live_chrome()
    print("    已持久化，len(cookie) =", len(cookie))
    if not get_bilibili_cookie():
        _fail("持久化后读不到 B站 cookie")
    if not cookie.startswith("SESSDATA="):
        _fail("拼出的 cookie 串不以 SESSDATA= 开头")

    print("PASS: bb-browser 从真实 Chrome 导出 B站登录态全链路通过")


if __name__ == "__main__":
    main()

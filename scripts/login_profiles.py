"""独立登录 profile 首次登录脚本。

为每个需要登录的站点创建独立专用 profile 并打开浏览器供扫码登录。
登录态存入 runtime/browser_profiles/<site>-auth/，后续抓取直接挂载该 profile。

用法:
    python scripts/login_profiles.py douyin
    python scripts/login_profiles.py xiaoheihe
    python scripts/login_profiles.py x
    python scripts/login_profiles.py youtube
    python scripts/login_profiles.py alphapai
    python scripts/login_profiles.py all       # 依次登录全部
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from connectors._shared.chrome_runner import _resolve_default_browser_executable
from connectors._shared.common import USER_AGENT
from connectors.auth.providers.browser_profiles import (
    ALPHAPAI_AUTH_PROFILE_DIR,
    DOUYIN_AUTH_PROFILE_DIR,
    X_AUTH_PROFILE_DIR,
    XIAOHEIHE_AUTH_PROFILE_DIR,
    YOUTUBE_AUTH_PROFILE_DIR,
)


SITES = {
    "douyin": (DOUYIN_AUTH_PROFILE_DIR, "https://www.douyin.com/"),
    "xiaoheihe": (XIAOHEIHE_AUTH_PROFILE_DIR, "https://www.xiaoheihe.cn/"),
    "x": (X_AUTH_PROFILE_DIR, "https://x.com/"),
    "youtube": (YOUTUBE_AUTH_PROFILE_DIR, "https://www.youtube.com/"),
    "alphapai": (ALPHAPAI_AUTH_PROFILE_DIR, "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"),
}


def login_site(site: str) -> int:
    if site not in SITES:
        print(f"未知站点: {site}，可选: {', '.join(SITES)}")
        return 1
    profile_dir, url = SITES[site]
    profile_dir.mkdir(parents=True, exist_ok=True)
    _, chrome_executable = _resolve_default_browser_executable()
    print(f"=== 登录 {site} ===")
    print(f"profile: {profile_dir}")
    print(f"打开 {url}，请在浏览器中完成登录，登录成功后关闭浏览器窗口或按 Ctrl+C 退出。")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=chrome_executable,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--no-first-run",
                f"--user-agent={USER_AGENT}",
            ],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print("浏览器已打开，等待登录...（关闭浏览器窗口结束）")
        try:
            # 阻塞直到用户关闭浏览器窗口
            while True:
                try:
                    pages = ctx.pages
                    if not pages:
                        break
                except Exception:
                    break
                import time as _t
                _t.sleep(2)
        except (KeyboardInterrupt, Exception):
            pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    print(f"=== {site} 登录态已存入 {profile_dir} ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="独立登录 profile 首次登录")
    parser.add_argument("site", help=f"站点名: {', '.join(SITES)} 或 all")
    args = parser.parse_args()
    if args.site == "all":
        for site in SITES:
            login_site(site)
        return 0
    return login_site(args.site)


if __name__ == "__main__":
    raise SystemExit(main())

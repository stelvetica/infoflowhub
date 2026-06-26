"""共享登录 profile 登录脚本。

所有浏览器站点共用一份 profile（runtime/browser_profiles/auth/），
cookie 按域名隔离互不干扰。一次打开浏览器，在多个 tab 里登录各站点。

用法:
    python scripts/login_profiles.py            # 打开浏览器，多 tab 登录全部站点
    python scripts/login_profiles.py douyin      # 只登录指定站点（可选: douyin xiaoheihe x youtube alphapai）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from connectors._shared.chrome_runner import _resolve_default_browser_executable
from connectors._shared.common import USER_AGENT
from connectors.auth.providers.browser_profiles import AUTH_PROFILE_DIR


SITES = {
    "douyin": "https://www.douyin.com/",
    "xiaoheihe": "https://www.xiaoheihe.cn/",
    "x": "https://x.com/",
    "youtube": "https://www.youtube.com/",
    "alphapai": "https://alphapai-web.rabyte.cn/reading/home/market-report/detail",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="共享登录 profile 登录")
    parser.add_argument("site", nargs="?", default="", help=f"单个站点（可选: {', '.join(SITES)}），留空则打开全部")
    args = parser.parse_args()

    AUTH_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _, chrome_executable = _resolve_default_browser_executable()

    if args.site:
        if args.site not in SITES:
            print(f"未知站点: {args.site}，可选: {', '.join(SITES)}")
            return 1
        targets = {args.site: SITES[args.site]}
    else:
        targets = SITES

    print(f"=== 共享登录 profile: {AUTH_PROFILE_DIR} ===")
    print(f"将在浏览器中打开 {len(targets)} 个站点，请分别登录后关闭浏览器窗口结束。")
    print("登录态存入同一 profile，cookie 按域名隔离互不干扰。")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(AUTH_PROFILE_DIR),
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
        for name, url in targets.items():
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"  已打开 {name}: {url}")
        print("浏览器已打开，等待登录...（关闭浏览器窗口结束）")
        try:
            while True:
                try:
                    if not ctx.pages:
                        break
                except Exception:
                    break
                time.sleep(2)
        except (KeyboardInterrupt, Exception):
            pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    print(f"=== 登录态已存入 {AUTH_PROFILE_DIR} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

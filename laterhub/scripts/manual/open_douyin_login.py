from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from laterhub.connectors.sites.douyin.fetch import DOUYIN_FAVORITE_URL, _resolve_default_browser_executable
from laterhub.services.config import PW_DOUYIN_PROFILE


def main() -> None:
    PW_DOUYIN_PROFILE.mkdir(parents=True, exist_ok=True)
    prog_id, executable = _resolve_default_browser_executable()
    print(f"使用浏览器: {prog_id} -> {executable}")
    print("将打开抖音收藏页。请在弹出的窗口中完成登录，完成后直接关闭浏览器窗口即可。")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PW_DOUYIN_PROFILE),
            executable_path=executable,
            headless=False,
            args=["--new-window"],
        )
        page = context.new_page()
        page.goto(DOUYIN_FAVORITE_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_timeout(600000)
        finally:
            context.close()


if __name__ == "__main__":
    main()

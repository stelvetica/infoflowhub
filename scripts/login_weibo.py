from __future__ import annotations

from playwright.sync_api import sync_playwright

from connectors._shared.common import USER_AGENT
from connectors.auth import get_auth_context_path


WEIBO_HOME = "https://weibo.com/login.php"


def main() -> None:
    profile_dir = get_auth_context_path("weibo_shared")
    profile_dir.mkdir(parents=True, exist_ok=True)
    print("将打开微博登录页。请在弹出的窗口中完成登录，完成后直接关闭浏览器窗口即可。")
    print(f"登录态目录: {profile_dir}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--new-window", "--window-size=1440,960"],
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        page.goto(WEIBO_HOME, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_timeout(600000)
        finally:
            context.close()


if __name__ == "__main__":
    main()

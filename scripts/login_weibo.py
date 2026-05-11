from __future__ import annotations

from playwright.sync_api import sync_playwright

from connectors.web.fetch import USER_AGENT, WEIBO_PROFILE_DIR


WEIBO_HOME = "https://weibo.com/login.php"


def main() -> None:
    WEIBO_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("将打开微博登录页。请在弹出的窗口中完成登录，完成后直接关闭浏览器窗口即可。")
    print(f"登录态目录: {WEIBO_PROFILE_DIR}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(WEIBO_PROFILE_DIR),
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

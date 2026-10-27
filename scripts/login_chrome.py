"""在常用 Chrome 中拉起所有需要登录的站点，供手动登录。

登录态最终存于常用 Chrome 的 Default profile（复制登录态的来源）。
关闭 ABE 后，需在此重新登录各站点，才会生成可复制的 cookie。

用法:
    python scripts/login_chrome.py            # 在默认浏览器拉起全部站点
    python scripts/login_chrome.py douyin     # 只拉起指定站点
"""
from __future__ import annotations

import subprocess
import sys

# 需要登录的站点（登录态存于常用 Chrome Default profile）
SITES = {
    "douyin": "https://www.douyin.com/",
    "xiaoheihe": "https://www.xiaoheihe.cn/",
    "weread": "https://weread.qq.com/web/shelf",
    "x": "https://x.com/",
    "youtube": "https://www.youtube.com/",
    "bilibili": "https://www.bilibili.com/",
    "alphapai": "https://alphapai-web.rabyte.cn/reading/home/market-report/detail",
}


def _open_url(url: str) -> None:
    """用系统默认浏览器打开 URL（拉起常用 Chrome 的新标签页）。"""
    try:
        subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    except OSError:
        subprocess.Popen(["start", url], shell=True)


def main() -> int:
    argv = sys.argv[1:]
    if argv:
        targets = {k: v for k, v in SITES.items() if k in argv}
        if not targets:
            print(f"未知站点: {argv}，可选: {', '.join(SITES)}")
            return 1
    else:
        targets = SITES

    print(f"将在常用 Chrome 中拉起 {len(targets)} 个站点登录页，请逐站登录：")
    for name, url in targets.items():
        print(f"  - {name}: {url}")
        _open_url(url)
    print("登录完成后关闭标签页即可。登录态存于常用 Chrome Default profile。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
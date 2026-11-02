"""浏览器可执行文件解析（Chrome 优先）。

copy/共享 profile 管线（CopyRunnerSession / SharedRunnerSession、profile 复制、Google
状态剥离、debug browser 保活）已随 copy 后端整体删除；本模块只保留 bsk 与登录脚本
仍然需要的「定位本机 Chromium 可执行文件」能力。
"""

from __future__ import annotations

from pathlib import Path

# 抓取期间会关掉用户常用 Chrome 时的重开地址；开机自启流程也用它打开控制台。
INFOFLOW_WEB_URL = "http://127.0.0.1:18421/"


WINDOWS_BROWSER_PATHS = {
    "ChromeHTML": [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ],
    "MSEdgeHTM": [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ],
    "BraveHTML": [
        Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ],
    "VivaldiHTM": [
        Path(r"C:\Program Files\Vivaldi\Application\vivaldi.exe"),
        Path(r"C:\Program Files (x86)\Vivaldi\Application\vivaldi.exe"),
    ],
}


def _resolve_default_browser_executable() -> tuple[str, str]:
    # 全局统一用 Chrome：只要本机装了 Chrome 就优先选它，不跟随系统默认浏览器
    # （系统默认可能是 Edge，会与 Chrome 登录态/扩展不兼容）。
    for prog_id in ("ChromeHTML", "MSEdgeHTM", "BraveHTML", "VivaldiHTM"):
        for candidate in WINDOWS_BROWSER_PATHS[prog_id]:
            if candidate.exists():
                return prog_id, str(candidate)

    raise RuntimeError("未找到可供 Playwright 启动的 Chromium 浏览器，请先安装 Chrome、Edge、Brave 或 Vivaldi")

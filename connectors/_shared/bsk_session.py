"""bsk 会话适配层：把 BskBrowser 包成与 CopyRunnerSession 同形的最小会话对象，
并提供「bsk 失败 → 重试 N 次 → 返回可读失败结果」的统一入口（不再自动回退 copy）。

放在独立小文件而非 bsk_browser.py：那边只管 bsk CLI 原语，这里只管会话形状与重试策略。
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Callable
from urllib.parse import urlparse

from apps.subscriptions.models import FeedFetchResult
from apps.subscriptions.rss_config import (
    BSK_FAILURE_MARKER,
    browser_command_timeout,
    browser_request_help_timeout,
    bsk_attempts,
)
from connectors._shared.bsk_browser import BskBrowser, BskError
from connectors._shared.common import result_error

# 子进程硬超时必须比 bsk 自身 --timeout 更宽：让 bsk 先返回并给出错误码，
# 被我们 kill 掉会丢掉 bsk 的错误分类（只剩一个「超时」）。
SLOW_SUBPROCESS_SLACK_SECONDS = 30.0
# 重试间隔：给 bsk daemon / 页面一个短暂恢复窗口，避免连续瞬间重试。
BSK_RETRY_DELAY_SECONDS = 2.0


class BskRunnerSession:
    """与 CopyRunnerSession 同形的最小 bsk 会话。

    只提供 start / shutdown / restart / acquire_page / acquire_page_by_url，
    因此 web_fetch 里既有的 `session.acquire_page_by_url(...)` 调用姿势无需改动。
    """

    def __init__(
        self,
        *,
        start_url: str = "about:blank",
        request_help_timeout: float | None = None,
        command_timeout: float | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        slow = float(command_timeout) if command_timeout is not None else browser_command_timeout()
        self.start_url = start_url
        self._browser = BskBrowser(
            timeout=slow + SLOW_SUBPROCESS_SLACK_SECONDS,
            slow_command_timeout=slow,
            request_help_timeout=(
                browser_request_help_timeout() if request_help_timeout is None else float(request_help_timeout)
            ),
            logger=logger,
        )
        self._started = False

    @property
    def browser(self) -> BskBrowser:
        return self._browser

    def start(self) -> None:
        if self._started:
            return
        self._browser.start()
        self._started = True
        if self.start_url and self.start_url != "about:blank":
            # 与 CopyRunnerSession 一致：会话起好后把 tab 落到目标页，后续 acquire_page_by_url
            # 命中同域即可直接复用，省掉一次往返。
            self._browser.navigate(self.start_url, wait_until="domcontentloaded")

    def shutdown(self, *, _reopen_chrome: bool = True) -> None:
        # _reopen_chrome 仅为与 CopyRunnerSession 同形：bsk 驱动的是用户自己已登录的浏览器，
        # 全程不关它，也就无所谓重开。
        self._browser.stop()
        self._started = False

    def restart(self) -> None:
        # bsk 直接用用户浏览器的登录态，没有「副本登录态失效」问题，重建会话即可。
        self.shutdown()
        self.start()

    def acquire_page(self):
        return self._browser.page()

    def acquire_page_by_url(self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 30000):
        # timeout_ms 是 CopyRunnerSession 的毫秒语义，这里有意忽略：bsk 导航的硬超时统一由
        # slow_command_timeout 控制（30s 对冷启动 SPA 太紧，正是要放宽的那一类）。
        page = self._browser.page()
        try:
            current_url = page.url
        except BskError:
            # 新会话的 tab 上扩展内容脚本还没注入，读 URL 走 evaluate → cooperate sync 会失败
            # （Could not establish connection）。读不到就当「不在目标页」，直接导航——导航是
            # 会话的第一个动作，允许不带控制上下文；若会话真的坏了，随后的导航会给出真错误。
            current_url = ""
        if not _same_host(current_url, url):
            page.goto(url, wait_until=wait_until)
        return page

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()
        return False


def _same_host(current_url: str, target_url: str) -> bool:
    current_host = urlparse(current_url or "").hostname or ""
    target_host = urlparse(target_url or "").hostname or ""
    return bool(current_host) and current_host == target_host


def run_bsk_with_retry(
    *,
    source: dict,
    bsk_session: BskRunnerSession,
    run_once: Callable[[Any], FeedFetchResult],
    attempts: int | None = None,
    manage_session: bool = True,
) -> FeedFetchResult:
    """在 bsk 会话上重试抓取；仍失败则返回可读失败结果——绝不自动回退 copy。

    copy 会带着 Google 身份/会话从第二个 profile 路径联网，触发账号双活吊销，
    并会 taskkill 强杀用户 Chrome，因此只作为用户在设置页显式触发的最后手段。
    无论重试几次都只返回一个 FeedFetchResult——调用方据此落库，同一源不会重复保存条目。

    manage_session=False：调用方已持有并启动了一个跨源复用的会话（如批量抓取 B 站多源），
    本函数只重试、不 start/shutdown，避免把共享会话拆掉。
    """
    total = max(1, int(bsk_attempts() if attempts is None else attempts))
    last_result: FeedFetchResult | None = None
    bsk_error = ""
    for attempt in range(1, total + 1):
        if attempt > 1:
            time.sleep(BSK_RETRY_DELAY_SECONDS)
        try:
            if manage_session:
                bsk_session.start()
            result = run_once(bsk_session)
            if result.ok:
                return result
            # feed.py 会把页面异常吞成非 ok 结果，所以「bsk 失败」不能只看异常。
            bsk_error = str(result.error or f"status={result.status}")
            last_result = result
        except Exception as exc:
            bsk_error = f"{type(exc).__name__}: {exc}"
            last_result = None
        finally:
            if manage_session:
                bsk_session.shutdown()
    error = _bsk_failure_error(total, bsk_error)
    if last_result is not None:
        return replace(last_result, error=error)
    return result_error(source, error)


def _bsk_failure_error(attempts: int, bsk_error: str) -> str:
    return f"{BSK_FAILURE_MARKER}（已尝试 {attempts} 次，未回退 copy）：{bsk_error}"
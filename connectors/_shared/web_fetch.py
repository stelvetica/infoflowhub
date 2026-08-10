from __future__ import annotations

import time

from apps.subscriptions.models import FeedFetchResult
from connectors._shared.chrome_runner import LiveChromeSession
from connectors._shared.common import (
    is_transient_fetch_error,
    resolve_web_target,
    result_error,
    validate_douyin_login_prerequisite,
    validate_x_login_prerequisite,
)
from connectors.alphapai import fetch_alphapai_with_page
from connectors.bilibili import fetch_bilibili_dynamic_feed
from connectors.douyin import fetch_douyin_subscription_with_page
from connectors.jiuyangongshe import fetch_jiuyangongshe_with_page
from connectors.wechat import fetch_wechat_feed
from connectors.x import fetch_x_with_page
from connectors.youtube import fetch_youtube_with_page

WEB_RETRY_DELAYS = (0.0, 2.0)

_SHARED_RUNNER_SITES = {"x", "youtube", "douyin", "alphapai"}


def _needs_profile_rebuild(result: FeedFetchResult) -> bool:
    error_text = str(result.error or "").lower()
    if result.status == 401:
        return True
    return "登录态失效" in error_text or "登录" in error_text or "login" in error_text


def _preflight_login_check(source: dict, site: str) -> str:
    if site == "x":
        return validate_x_login_prerequisite(source)
    if site == "douyin":
        return validate_douyin_login_prerequisite(source)
    return ""


def _fetch_via_shared_runner(
    session: LiveChromeSession,
    source: dict,
    fetch_fn,
    *,
    limit: int = 12,
    timeout_ms: int = 60000,
    use_url_tab: bool = False,
) -> FeedFetchResult:
    """在复制副本 session 上抓一个源。use_url_tab=True 时复用/打开目标 URL 的 tab（蓝宝书用）。"""
    target = resolve_web_target(source)
    start_url = target.page_url if target else ""
    if use_url_tab:
        page = session.acquire_page_by_url(start_url, timeout_ms=30000)
    else:
        page = session.acquire_page()
    try:
        result = fetch_fn(page, source, timeout_ms=timeout_ms, limit=limit)
        if _needs_profile_rebuild(result):
            session.restart()
            if use_url_tab:
                page = session.acquire_page_by_url(start_url, timeout_ms=30000)
            else:
                page = session.acquire_page()
            result = fetch_fn(page, source, timeout_ms=timeout_ms, limit=limit)
        return result
    finally:
        try:
            page.close()
        except Exception:
            pass


def _fetch_alphapai_via_session(session: LiveChromeSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    return _fetch_via_shared_runner(session, source, fetch_alphapai_with_page, limit=limit, timeout_ms=timeout_ms, use_url_tab=True)


def _fetch_one_via_session(session: LiveChromeSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    """在已启动的复制副本 session 上抓单个源（含蓝宝书）。"""
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的网页直抓源")

    if target.site == "bilibili":
        return fetch_bilibili_dynamic_feed(source, limit=limit, timeout_ms=timeout_ms)

    if target.site == "wechat":
        return fetch_wechat_feed(source, limit=limit)

    if target.site == "alphapai":
        return _fetch_alphapai_via_session(session, source, limit=limit, timeout_ms=timeout_ms)

    if target.site == "jiuyangongshe":
        return fetch_jiuyangongshe_with_page(None, source, limit=limit, timeout_ms=timeout_ms)

    if target.site in {"x", "youtube", "douyin"}:
        login_error = _preflight_login_check(source, target.site)
        if login_error:
            return result_error(source, login_error)
        fetch_fn = {
            "x": fetch_x_with_page,
            "youtube": fetch_youtube_with_page,
            "douyin": fetch_douyin_subscription_with_page,
        }[target.site]
        return _fetch_via_shared_runner(session, source, fetch_fn, limit=limit, timeout_ms=timeout_ms)

    return result_error(source, "暂不支持的网页直抓源")


def _should_retry_web_result(result: FeedFetchResult) -> bool:
    return (not result.ok) and is_transient_fetch_error(result.error or str(result.status))


def _needs_shared_runner(source: dict) -> bool:
    target = resolve_web_target(source)
    return bool(target and target.site in _SHARED_RUNNER_SITES)


def _make_session_for(source: dict) -> LiveChromeSession:
    """订阅抓取走 LiveChromeSession：经 bb-browser 直连真实 Chrome 登录态。

    各站点（X/YouTube/抖音/蓝宝书）共用同一真实 Chrome，cookie 按域名隔离天然
    可用。替代原 CopyRunnerSession（关 Chrome + 复制 profile）：不关 Chrome、
    不复制、不重登，仍需已关 ABE 且日常 Chrome 运行。
    """
    return LiveChromeSession(start_url="about:blank")


def fetch_web_source(source: dict) -> FeedFetchResult:
    """单源抓取（CLI/重试入口）。自起临时 session。"""
    last_result: FeedFetchResult | None = None
    for attempt, delay in enumerate(WEB_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            if _needs_shared_runner(source):
                with _make_session_for(source) as session:
                    result = _fetch_one_via_session(session, source)
            else:
                target = resolve_web_target(source)
                if not target:
                    result = result_error(source, "暂不支持的网页直抓源")
                elif target.site == "bilibili":
                    result = fetch_bilibili_dynamic_feed(source)
                elif target.site == "wechat":
                    result = fetch_wechat_feed(source, limit=12)
                elif target.site == "jiuyangongshe":
                    result = fetch_jiuyangongshe_with_page(None, source)
                else:
                    result = result_error(source, "暂不支持的网页直抓源")
        except Exception as exc:
            result = result_error(source, f"网页直抓失败: {exc}")
        last_result = result
        if not _should_retry_web_result(result) or attempt >= len(WEB_RETRY_DELAYS):
            return result
    return last_result or result_error(source, "网页直抓失败")


def fetch_web_many(
    sources: list[dict],
    limit: int = 12,
    timeout_ms: int = 60000,
    session: LiveChromeSession | None = None,
) -> list[FeedFetchResult]:
    """批量抓取。所有浏览器站点共用一个复制副本 session（单一常用 Chrome 登录态来源）。"""
    if not sources:
        return []

    results_by_index: dict[int, FeedFetchResult] = {}
    shared_indices = [i for i, s in enumerate(sources) if _needs_shared_runner(s)]
    other_indices = [i for i in range(len(sources)) if i not in set(shared_indices)]

    # 所有浏览器站点共用一个复制副本 session（单一常用 Chrome 登录态来源）
    if shared_indices:
        session = _make_session_for(sources[shared_indices[0]])
        session.start()
        try:
            for i in shared_indices:
                source = sources[i]
                try:
                    results_by_index[i] = _fetch_one_via_session(session, source, limit=limit, timeout_ms=timeout_ms)
                except Exception as exc:
                    results_by_index[i] = result_error(source, f"网页直抓失败: {exc}")
        finally:
            session.shutdown()

    # bilibili / wechat 等非浏览器源各自独立抓取
    for i in other_indices:
        source = sources[i]
        last_result: FeedFetchResult | None = None
        for attempt, delay in enumerate(WEB_RETRY_DELAYS, start=1):
            if delay:
                time.sleep(delay)
            try:
                target = resolve_web_target(source)
                if not target:
                    result = result_error(source, "暂不支持的网页直抓源")
                elif target.site == "bilibili":
                    result = fetch_bilibili_dynamic_feed(source, limit=limit, timeout_ms=timeout_ms)
                elif target.site == "wechat":
                    result = fetch_wechat_feed(source, limit=limit)
                elif target.site == "jiuyangongshe":
                    result = fetch_jiuyangongshe_with_page(None, source, limit=limit, timeout_ms=timeout_ms)
                else:
                    result = result_error(source, "暂不支持的网页直抓源")
            except Exception as exc:
                result = result_error(source, f"网页直抓失败: {exc}")
            last_result = result
            if not _should_retry_web_result(result) or attempt >= len(WEB_RETRY_DELAYS):
                break
        results_by_index[i] = last_result or result_error(source, "网页直抓失败")

    return [results_by_index[i] for i in range(len(sources))]

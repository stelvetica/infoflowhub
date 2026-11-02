from __future__ import annotations

import time

from apps.subscriptions.models import FeedFetchResult
from connectors._shared.bsk_session import BskRunnerSession, run_bsk_with_retry
from connectors._shared.common import (
    is_transient_fetch_error,
    resolve_web_target,
    result_error,
    validate_douyin_login_prerequisite,
    validate_x_login_prerequisite,
)
from connectors.alphapai import fetch_alphapai_social_with_page, fetch_alphapai_with_page
from connectors.bilibili import fetch_bilibili_dynamic_feed_with_page
from connectors.douyin import fetch_douyin_subscription_with_page
from connectors.jiuyangongshe import fetch_jiuyangongshe_with_page
from connectors.x import fetch_x_with_page
from connectors.xiaoheihe import fetch_xiaoheihe_profile_with_page
from connectors.youtube import fetch_youtube_with_page

WEB_RETRY_DELAYS = (0.0, 2.0)

_SHARED_RUNNER_SITES = {"x", "youtube", "douyin", "alphapai", "alphapai_social", "xiaoheihe"}


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
    session: BskRunnerSession,
    source: dict,
    fetch_fn,
    *,
    limit: int = 12,
    timeout_ms: int = 60000,
    use_url_tab: bool = False,
) -> FeedFetchResult:
    """在 bsk 会话上抓一个源。use_url_tab=True 时先导航到目标 URL（蓝宝书用）。"""
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


def _fetch_alphapai_via_session(session: BskRunnerSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    return _fetch_via_shared_runner(session, source, fetch_alphapai_with_page, limit=limit, timeout_ms=timeout_ms, use_url_tab=True)


def _fetch_alphapai_social_via_session(session: BskRunnerSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    return _fetch_via_shared_runner(session, source, fetch_alphapai_social_with_page, limit=limit, timeout_ms=timeout_ms, use_url_tab=True)


def _fetch_one_via_session(session: BskRunnerSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    """在已启动的 bsk 会话上抓单个源（含蓝宝书）。"""
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的网页直抓源")

    if target.site == "bilibili":
        return _fetch_bilibili_via_bsk_session(session, source, limit=limit, timeout_ms=timeout_ms)

    if target.site == "alphapai":
        return _fetch_alphapai_via_session(session, source, limit=limit, timeout_ms=timeout_ms)

    if target.site == "alphapai_social":
        return _fetch_alphapai_social_via_session(session, source, limit=limit, timeout_ms=timeout_ms)

    if target.site == "jiuyangongshe":
        return fetch_jiuyangongshe_with_page(None, source, limit=limit, timeout_ms=timeout_ms)

    if target.site in {"x", "youtube", "douyin", "xiaoheihe"}:
        login_error = _preflight_login_check(source, target.site)
        if login_error:
            return result_error(source, login_error)
        fetch_fn = {
            "x": fetch_x_with_page,
            "youtube": fetch_youtube_with_page,
            "douyin": fetch_douyin_subscription_with_page,
            "xiaoheihe": fetch_xiaoheihe_profile_with_page,
        }[target.site]
        return _fetch_via_shared_runner(session, source, fetch_fn, limit=limit, timeout_ms=timeout_ms)

    return result_error(source, "暂不支持的网页直抓源")


def _should_retry_web_result(result: FeedFetchResult) -> bool:
    return (not result.ok) and is_transient_fetch_error(result.error or str(result.status))


def _needs_shared_runner(source: dict) -> bool:
    target = resolve_web_target(source)
    return bool(target and target.site in _SHARED_RUNNER_SITES)


def _make_session_for(source: dict) -> BskRunnerSession:
    """唯一的会话构造点：浏览器抓取一律走 bsk（copy 已整体禁用）。"""
    return BskRunnerSession(start_url="about:blank")


def _is_bilibili(source: dict) -> bool:
    target = resolve_web_target(source)
    return bool(target and target.site == "bilibili")


def _fetch_bilibili_via_bsk_session(
    session: BskRunnerSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000
) -> FeedFetchResult:
    """在已启动的 bsk 会话上抓一个 B 站源：每次显式导航到该源的 space 页。

    不依赖 acquire_page_by_url 的同域复用（12 个源同属 space.bilibili.com，
    复用会跳过导航导致抓到上一个源的动态）。
    """
    page = session.acquire_page()
    return fetch_bilibili_dynamic_feed_with_page(page, source, limit=limit, timeout_ms=timeout_ms)


def _fetch_bilibili_one(source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    """B 站专用分派（唯一分支点）：一律走 bsk 页内 fetch（复用用户浏览器真实 cookie）。

    B 站不在 _SHARED_RUNNER_SITES 里，由批量抓取单独分组复用同一个 bsk 会话。
    """
    return run_bsk_with_retry(
        source=source,
        bsk_session=_make_session_for(source),
        run_once=lambda session: _fetch_bilibili_via_bsk_session(
            session, source, limit=limit, timeout_ms=timeout_ms
        ),
    )


def _fetch_one_with_backend(source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    """抓单个源：一律走 bsk，失败重试配置次数后返回可读失败结果。"""
    return run_bsk_with_retry(
        source=source,
        bsk_session=_make_session_for(source),
        run_once=lambda session: _fetch_one_via_session(session, source, limit=limit, timeout_ms=timeout_ms),
    )


def fetch_web_source(source: dict) -> FeedFetchResult:
    """单源抓取（CLI/重试入口）。自起临时 session。"""
    last_result: FeedFetchResult | None = None
    for attempt, delay in enumerate(WEB_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            if _needs_shared_runner(source):
                result = _fetch_one_with_backend(source)
            else:
                target = resolve_web_target(source)
                if not target:
                    result = result_error(source, "暂不支持的网页直抓源")
                elif target.site == "bilibili":
                    result = _fetch_bilibili_one(source)
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
) -> list[FeedFetchResult]:
    """批量抓取。浏览器源一律走 bsk；每个共享站点源独立会话，B 站多源共用一个会话。"""
    if not sources:
        return []

    results_by_index: dict[int, FeedFetchResult] = {}
    shared_indices = [i for i, s in enumerate(sources) if _needs_shared_runner(s)]
    # B 站源单独成组：12 个源共用一个 bsk 会话（一次 start/stop），每源各导航自己的 space 页。
    # 它们不进 shared_indices（B 站不在 _SHARED_RUNNER_SITES）。
    bili_bsk_indices = [
        i for i in range(len(sources)) if i not in set(shared_indices) and _is_bilibili(sources[i])
    ]
    other_indices = [
        i for i in range(len(sources)) if i not in set(shared_indices) and i not in set(bili_bsk_indices)
    ]

    # 共享站点：每个源一个独立会话（失败只重试）
    for i in shared_indices:
        source = sources[i]
        try:
            results_by_index[i] = _fetch_one_with_backend(source, limit=limit, timeout_ms=timeout_ms)
        except Exception as exc:
            results_by_index[i] = result_error(source, f"网页直抓失败: {exc}")

    # B 站 bsk 组：共用一个 bsk 会话，省掉每源一次 start/stop（12 次往返）。
    # 会话起不来只作各源失败结果，不炸穿整批。
    if bili_bsk_indices:
        bili_session: BskRunnerSession | None = None
        start_error = ""
        try:
            bili_session = _make_session_for(sources[bili_bsk_indices[0]])
            bili_session.start()
        except Exception as exc:
            start_error = f"浏览器会话启动失败: {exc}"
            bili_session = None
        if bili_session is None:
            for i in bili_bsk_indices:
                results_by_index[i] = result_error(sources[i], start_error)
        else:
            try:
                for i in bili_bsk_indices:
                    source = sources[i]
                    results_by_index[i] = run_bsk_with_retry(
                        source=source,
                        bsk_session=bili_session,
                        run_once=lambda session, source=source: _fetch_bilibili_via_bsk_session(
                            session, source, limit=limit, timeout_ms=timeout_ms
                        ),
                        manage_session=False,
                    )
            finally:
                bili_session.shutdown()

    # bilibili 等非浏览器源各自独立抓取
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
                    result = _fetch_bilibili_one(source, limit=limit, timeout_ms=timeout_ms)
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

from __future__ import annotations

import time
from pathlib import Path

from apps.subscriptions.models import FeedFetchResult
from apps.subscriptions.rss_config import browser_backend_for
from connectors._shared.bsk_session import BskRunnerSession, run_bsk_with_retry
from connectors._shared.chrome_runner import CopyRunnerSession, SharedRunnerSession
from connectors._shared.common import (
    USER_AGENT,
    is_transient_fetch_error,
    resolve_web_target,
    result_error,
    validate_douyin_login_prerequisite,
    validate_x_login_prerequisite,
)
from connectors.alphapai import fetch_alphapai_social_with_page, fetch_alphapai_with_page
from connectors.alphapai.browser import ALPHAPAI_TARGET_URL
from connectors.bilibili import fetch_bilibili_dynamic_feed, fetch_bilibili_dynamic_feed_with_page
from connectors.douyin import fetch_douyin_subscription_with_page
from connectors.jiuyangongshe import fetch_jiuyangongshe_with_page
from connectors.x import fetch_x_with_page
from connectors.xiaoheihe import fetch_xiaoheihe_profile_with_page
from connectors.youtube import fetch_youtube_with_page

WEB_RETRY_DELAYS = (0.0, 2.0)
WEB_RUNNER_DEBUG_PORT = 9223

_SHARED_RUNNER_SITES = {"x", "youtube", "douyin", "alphapai", "alphapai_social", "xiaoheihe"}


def _needs_profile_rebuild(result: FeedFetchResult) -> bool:
    error_text = str(result.error or "").lower()
    if result.status == 401:
        return True
    return "登录态失效" in error_text or "登录" in error_text or "login" in error_text


def _shared_runner_extra_args() -> list[str]:
    # 共享 runner 只起一次，统一用一个兼顾中英文的 Accept-Language
    return [f"--user-agent={USER_AGENT}", "--lang=zh-CN,zh;q=0.9,en;q=0.8"]


def _shared_runner_source_profile_dir(site: str) -> Path:
    """所有浏览器站点统一复制常用 Chrome Default profile 登录态（cookie 按域名隔离）。"""
    from connectors.auth.providers.browser_profiles import _default_chrome_profile_dir
    return _default_chrome_profile_dir()


def _preflight_login_check(source: dict, site: str) -> str:
    if site == "x":
        return validate_x_login_prerequisite(source)
    if site == "douyin":
        return validate_douyin_login_prerequisite(source)
    return ""


def _fetch_via_shared_runner(
    session: CopyRunnerSession,
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


def _fetch_alphapai_via_session(session: CopyRunnerSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    return _fetch_via_shared_runner(session, source, fetch_alphapai_with_page, limit=limit, timeout_ms=timeout_ms, use_url_tab=True)


def _fetch_alphapai_social_via_session(session: CopyRunnerSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    return _fetch_via_shared_runner(session, source, fetch_alphapai_social_with_page, limit=limit, timeout_ms=timeout_ms, use_url_tab=True)


def _fetch_one_via_session(session: CopyRunnerSession, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    """在已启动的复制副本 session 上抓单个源（含蓝宝书）。"""
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的网页直抓源")

    if target.site == "bilibili":
        return fetch_bilibili_dynamic_feed(source, limit=limit, timeout_ms=timeout_ms)

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


def _make_copy_session_for(source: dict) -> CopyRunnerSession:
    """复制常用 Chrome Default profile 登录态到副本（历史默认后端，行为不变）。

    各站点（X/YouTube/抖音/蓝宝书）共用同一常用 Chrome 来源，cookie 按域名隔离天然可用。
    headless 无头抓取，副本独立运行不影响你常用浏览器。
    """
    from connectors.auth.providers.browser_profiles import WEB_RUNNER_DIR

    return CopyRunnerSession(
        runner_dir=WEB_RUNNER_DIR,
        debug_port=WEB_RUNNER_DEBUG_PORT,
        source_profile_dir=_shared_runner_source_profile_dir(""),
        start_url="about:blank",
        headless=True,
        extra_args=_shared_runner_extra_args(),
    )


def _uses_bsk(source: dict) -> bool:
    return browser_backend_for(source) == "bsk"


def _make_session_for(source: dict):
    """按配置挑会话（唯一分派点）：默认 copy（复制常用 Chrome Default profile 副本），
    可按 source id 覆盖成 bsk（驱动用户自己已登录的 Chromium，不复制 profile）。"""
    if _uses_bsk(source):
        return BskRunnerSession(start_url="about:blank")
    return _make_copy_session_for(source)


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
    """B 站专用分派（唯一分支点）：bsk 配置走页内 fetch（复用用户浏览器真实 cookie）；
    其余配置一律走 HTTP 路径（零浏览器、零 Google 风险）。

    这里绝不经过 _make_copy_session_for：B 站不在 _SHARED_RUNNER_SITES 里，也不会被
    copy_indices 选中，因此任何配置下都构造不出 copy 会话（copy 是设置页显式最后手段）。
    """
    if _uses_bsk(source):
        return run_bsk_with_retry(
            source=source,
            bsk_session=_make_session_for(source),
            run_once=lambda session: _fetch_bilibili_via_bsk_session(
                session, source, limit=limit, timeout_ms=timeout_ms
            ),
        )
    return fetch_bilibili_dynamic_feed(source, limit=limit, timeout_ms=timeout_ms)


def _fetch_one_with_backend(source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    """抓单个源，后端由配置决定；bsk 失败重试配置次数后返回可读失败结果，不自动回退 copy。"""
    if not _uses_bsk(source):
        with _make_session_for(source) as session:
            return _fetch_one_via_session(session, source, limit=limit, timeout_ms=timeout_ms)
    return run_bsk_with_retry(
        source=source,
        bsk_session=_make_session_for(source),
        run_once=lambda session: _fetch_one_via_session(session, source, limit=limit, timeout_ms=timeout_ms),
    )


def fetch_source_via_copy(source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    """显式按源跑一次 copy（复制 profile）路径。

    仅由设置页「用 copy 重试」按钮调用：bsk 反复失败后，经用户确认才用这个最后手段
    （copy 会带 Google 身份从第二个 profile 路径联网，可能吊销账号会话并强杀用户 Chrome）。

    B 站例外：它没有需要登录态的浏览器页面（取数就一个公开 API），copy 对它毫无增益，
    因此即便用户点了「用 copy 重试」也只走 HTTP 兜底——保证 B 站任何路径都不构造 copy 会话。
    """
    if _is_bilibili(source):
        return _fetch_bilibili_one(source, limit=limit, timeout_ms=timeout_ms)
    try:
        with _make_copy_session_for(source) as session:
            return _fetch_one_via_session(session, source, limit=limit, timeout_ms=timeout_ms)
    except Exception as exc:
        return result_error(source, f"copy 路径抓取失败: {exc}")


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
    session: CopyRunnerSession | None = None,
) -> list[FeedFetchResult]:
    """批量抓取。所有浏览器站点共用一个复制副本 session（单一常用 Chrome 登录态来源）。"""
    if not sources:
        return []

    results_by_index: dict[int, FeedFetchResult] = {}
    shared_indices = [i for i, s in enumerate(sources) if _needs_shared_runner(s)]
    bsk_indices = [i for i in shared_indices if _uses_bsk(sources[i])]
    copy_indices = [i for i in shared_indices if i not in set(bsk_indices)]
    # B 站 bsk 源单独成组：12 个源共用一个 bsk 会话（一次 start/stop），每源各导航自己的 space 页。
    # 它们不进 shared_indices（B 站不在 _SHARED_RUNNER_SITES），因此永远不会落到 copy 组。
    bili_bsk_indices = [
        i
        for i in range(len(sources))
        if i not in set(shared_indices) and _is_bilibili(sources[i]) and _uses_bsk(sources[i])
    ]
    other_indices = [
        i for i in range(len(sources)) if i not in set(shared_indices) and i not in set(bili_bsk_indices)
    ]

    # bsk 后端：每个源一个独立会话（失败只重试，不回退 copy），不与复制副本组共用
    for i in bsk_indices:
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

    # 复制副本后端：所有站点共用一个 session（单一常用 Chrome 登录态来源）
    # start 需关闭用户 Chrome 复制 profile，可能失败——失败只作各源错误，
    # 不得向上抛炸穿整批抓取（历史上曾丢弃同批已抓好的全部源）
    if copy_indices:
        session: CopyRunnerSession | None = None
        start_error = ""
        try:
            session = _make_session_for(sources[copy_indices[0]])
            session.start()
        except Exception as exc:
            start_error = f"浏览器会话启动失败: {exc}"
            session = None
        if session is None:
            for i in copy_indices:
                results_by_index[i] = result_error(sources[i], start_error)
        else:
            try:
                for i in copy_indices:
                    source = sources[i]
                    try:
                        results_by_index[i] = _fetch_one_via_session(session, source, limit=limit, timeout_ms=timeout_ms)
                    except Exception as exc:
                        results_by_index[i] = result_error(source, f"网页直抓失败: {exc}")
            finally:
                session.shutdown()

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

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from apps.subscriptions.models import FeedFetchResult
from connectors._shared.chrome_runner import SharedRunnerSession
from connectors._shared.common import (
    CHROME_USER_DATA,
    USER_AGENT,
    is_transient_fetch_error,
    resolve_web_target,
    result_error,
    validate_douyin_login_prerequisite,
    validate_x_login_prerequisite,
)
from connectors.alphapai import fetch_alphapai_with_page
from connectors.alphapai.browser import (
    ALPHAPAI_TARGET_URL,
    connect_over_cdp_endpoint,
    ensure_alphapai_debug_browser,
    find_alphapai_tab_url,
    force_rebuild_alphapai_debug_browser,
)
from connectors.auth import get_auth_context_path
from connectors.bilibili import fetch_bilibili_dynamic_feed
from connectors.douyin import fetch_douyin_subscription_with_page
from connectors.wechat import fetch_wechat_feed
from connectors.x import fetch_x_with_page
from connectors.youtube import fetch_youtube_with_page

WEB_RETRY_DELAYS = (0.0, 2.0)


def _needs_profile_rebuild(result: FeedFetchResult) -> bool:
    error_text = str(result.error or "").lower()
    if result.status == 401:
        return True
    return "登录态失效" in error_text or "登录" in error_text or "login" in error_text


def _resolve_douyin_source_profile_dir() -> Path:
    shared_profile_dir = get_auth_context_path("douyin_shared") / "Default"
    default_profile_dir = CHROME_USER_DATA / "Default"
    if (shared_profile_dir / "Network" / "Cookies").exists() and (shared_profile_dir / "Preferences").exists():
        return shared_profile_dir
    return default_profile_dir


def _shared_runner_extra_args(site: str) -> list[str]:
    # 共享 runner 只起一次，统一用一个兼顾中英文的 Accept-Language
    return [f"--user-agent={USER_AGENT}", "--lang=zh-CN,zh;q=0.9,en;q=0.8"]


def _fetch_via_shared_runner(
    session: SharedRunnerSession,
    source: dict,
    fetch_fn,
    *,
    limit: int = 12,
    timeout_ms: int = 60000,
) -> FeedFetchResult:
    page = session.acquire_page()
    try:
        result = fetch_fn(page, source, timeout_ms=timeout_ms, limit=limit)
        if _needs_profile_rebuild(result):
            session.shutdown()
            session.start()
            page = session.acquire_page()
            result = fetch_fn(page, source, timeout_ms=timeout_ms, limit=limit)
        return result
    finally:
        try:
            page.close()
        except Exception:
            pass


def _fetch_alphapai_with_runner(playwright, source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    try:
        ensure_alphapai_debug_browser()
    except Exception as exc:
        return result_error(source, f"蓝宝书浏览器准备失败: {exc}")
    browser = playwright.chromium.connect_over_cdp(connect_over_cdp_endpoint())
    try:
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(find_alphapai_tab_url() or ALPHAPAI_TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        result = fetch_alphapai_with_page(page, source, timeout_ms=timeout_ms, limit=limit)
        if not result.ok and _needs_profile_rebuild(result):
            browser.close()
            force_rebuild_alphapai_debug_browser()
            browser = playwright.chromium.connect_over_cdp(connect_over_cdp_endpoint())
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            page.goto(find_alphapai_tab_url() or ALPHAPAI_TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            result = fetch_alphapai_with_page(page, source, timeout_ms=timeout_ms, limit=limit)
        return result
    finally:
        browser.close()


def _fetch_single_source(source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的网页直抓源")

    if target.site == "bilibili":
        return fetch_bilibili_dynamic_feed(source, limit=limit, timeout_ms=timeout_ms)

    if target.site == "wechat":
        return fetch_wechat_feed(source, limit=limit)

    if target.site in _SHARED_RUNNER_SITES:
        login_error = _preflight_login_check(source, target.site)
        if login_error:
            return result_error(source, login_error)
        with SharedRunnerSession(
            source_profile_dir=_shared_runner_source_profile_dir(target.site),
            extra_args=_shared_runner_extra_args(target.site),
        ) as session:
            return _fetch_via_shared_runner(
                session, source, _shared_runner_fetch_fn(target.site), limit=limit, timeout_ms=timeout_ms
            )

    if target.site == "alphapai":
        with sync_playwright() as playwright:
            return _fetch_alphapai_with_runner(playwright, source, limit=limit, timeout_ms=timeout_ms)

    return result_error(source, "暂不支持的网页直抓源")


def _should_retry_web_result(result: FeedFetchResult) -> bool:
    return (not result.ok) and is_transient_fetch_error(result.error or str(result.status))


_SHARED_RUNNER_SITES = {"x", "youtube", "douyin"}


def _shared_runner_source_profile_dir(site: str) -> Path:
    if site == "douyin":
        return _resolve_douyin_source_profile_dir()
    return CHROME_USER_DATA / "Default"


def _shared_runner_fetch_fn(site: str):
    return {
        "x": fetch_x_with_page,
        "youtube": fetch_youtube_with_page,
        "douyin": fetch_douyin_subscription_with_page,
    }[site]


def _preflight_login_check(source: dict, site: str) -> str:
    if site == "x":
        return validate_x_login_prerequisite(source)
    if site == "douyin":
        return validate_douyin_login_prerequisite(source)
    return ""


def _fetch_web_source_once(playwright, source: dict, *, limit: int = 12, timeout_ms: int = 60000, session: SharedRunnerSession | None = None) -> FeedFetchResult:
    # 保留以兼容旧测试 mock；实际逻辑已迁至 _fetch_single_source
    return _fetch_single_source(source, limit=limit, timeout_ms=timeout_ms)


def _fetch_web_source_with_retry(source: dict, *, limit: int = 12, timeout_ms: int = 60000) -> FeedFetchResult:
    last_result: FeedFetchResult | None = None
    for attempt, delay in enumerate(WEB_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            result = _fetch_single_source(source, limit=limit, timeout_ms=timeout_ms)
        except Exception as exc:
            result = result_error(source, f"网页直抓失败: {exc}")
        last_result = result
        if not _should_retry_web_result(result) or attempt >= len(WEB_RETRY_DELAYS):
            return result
    return last_result or result_error(source, "网页直抓失败")


def fetch_web_source(source: dict) -> FeedFetchResult:
    return _fetch_web_source_with_retry(source)


def fetch_web_many(sources: list[dict], limit: int = 12, timeout_ms: int = 60000) -> list[FeedFetchResult]:
    if not sources:
        return []

    results_by_source: dict[int, FeedFetchResult] = {}
    shared_runner_sources = [
        (i, s) for i, s in enumerate(sources)
        if (target := resolve_web_target(s)) and target.site in _SHARED_RUNNER_SITES
    ]
    other_indices = [i for i in range(len(sources)) if i not in {idx for idx, _ in shared_runner_sources}]

    # 共享 runner 站点（抖音/X/YouTube）共用一个会话级 Chrome
    if shared_runner_sources:
        session: SharedRunnerSession | None = None
        for idx, source in shared_runner_sources:
            site = resolve_web_target(source).site
            login_error = _preflight_login_check(source, site)
            if login_error:
                results_by_source[idx] = result_error(source, login_error)
                continue
            try:
                if session is None:
                    session = SharedRunnerSession(
                        source_profile_dir=_shared_runner_source_profile_dir(site),
                        extra_args=_shared_runner_extra_args(site),
                    )
                if not session._started:  # noqa: SLF001
                    session.start()
                results_by_source[idx] = _fetch_via_shared_runner(
                    session, source, _shared_runner_fetch_fn(site), limit=limit, timeout_ms=timeout_ms
                )
            except Exception as exc:
                results_by_source[idx] = result_error(source, f"网页直抓失败: {exc}")
        if session is not None:
            session.shutdown()

    # 其余站点（bilibili / wechat / alphapai）各自独立抓取
    for idx in other_indices:
        source = sources[idx]
        results_by_source[idx] = _fetch_web_source_with_retry(source, limit=limit, timeout_ms=timeout_ms)

    return [results_by_source[i] for i in range(len(sources))]


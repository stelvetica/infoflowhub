from __future__ import annotations

from pathlib import Path

from apps.subscriptions.models import FeedFetchResult
from apps.subscriptions.rss_config import browser_backend_for
from apps.subscriptions.rss_db import save_entries
from connectors._shared.bsk_session import BskRunnerSession, run_bsk_with_copy_fallback
from connectors._shared.chrome_runner import CopyRunnerSession
from connectors._shared.common import CHROME_USER_DATA, USER_AGENT, result_error
from connectors.alphapai.browser import ALPHAPAI_TARGET_URL
from connectors.alphapai.feed import fetch_alphapai_with_page

DEBUG_DIR = Path(__file__).resolve().parents[2] / "runtime" / "debug"
ALPHAPAI_RUNNER_DIR = Path(__file__).resolve().parents[2] / "runtime" / "browser_profiles" / "alphapai-runner"
ALPHAPAI_DEBUG_PORT = 9222


def _write_debug(name: str, content: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / name).write_text(content, encoding="utf-8")


def _needs_profile_rebuild(result: FeedFetchResult) -> bool:
    error_text = str(result.error or "")
    if result.status == 401:
        return True
    return "登录态失效" in error_text or "登录" in error_text


def _make_session() -> CopyRunnerSession:
    # 蓝宝书单设备登录限制：复制本机 Chrome Default profile 登录态到副本，
    # 不在副本登录，副本独立运行不影响常用浏览器。
    return CopyRunnerSession(
        runner_dir=ALPHAPAI_RUNNER_DIR,
        debug_port=ALPHAPAI_DEBUG_PORT,
        source_profile_dir=CHROME_USER_DATA / "Default",
        start_url=ALPHAPAI_TARGET_URL,
        headless=True,
        extra_args=[f"--user-agent={USER_AGENT}", "--lang=zh-CN,zh;q=0.9,en;q=0.8"],
    )


def _make_bsk_session() -> BskRunnerSession:
    # bsk 驱动用户自己已登录的 Chromium，蓝宝书单设备登录限制天然满足，无需复制 profile。
    return BskRunnerSession(start_url=ALPHAPAI_TARGET_URL)


def _run_fetch_once(session: CopyRunnerSession, source: dict, *, limit: int, timeout_ms: int) -> FeedFetchResult:
    page = session.acquire_page_by_url(ALPHAPAI_TARGET_URL, timeout_ms=30000)
    try:
        result = fetch_alphapai_with_page(page, source, timeout_ms=timeout_ms, limit=limit)
        if not result.ok:
            try:
                _write_debug("alphapai_last_error_url.txt", str(page.url or ""))
                _write_debug("alphapai_last_error_html.html", page.content())
            except Exception:
                pass
        if _needs_profile_rebuild(result):
            session.restart()
            page = session.acquire_page_by_url(ALPHAPAI_TARGET_URL, timeout_ms=30000)
            result = fetch_alphapai_with_page(page, source, timeout_ms=timeout_ms, limit=limit)
        return result
    finally:
        try:
            page.close()
        except Exception:
            pass


def fetch_alphapai_source(
    source: dict,
    *,
    limit: int = 12,
    timeout_ms: int = 120000,
    session=None,
) -> FeedFetchResult:
    if session is None and browser_backend_for(source) == "bsk":
        # bsk 失败（BskError 任一子类/超时/登录帮助阻塞，或被 feed.py 吞成非 ok 结果）
        # 时结束 bsk 会话，回退到复制 profile 路径重试一次；只返回一个结果，故只落一次库。
        return run_bsk_with_copy_fallback(
            source=source,
            bsk_session=_make_bsk_session(),
            copy_session_factory=_make_session,
            run_once=lambda active: _run_fetch_once(active, source, limit=limit, timeout_ms=timeout_ms),
        )
    own_session = session is None
    session = session or _make_session()
    try:
        # start（需关闭用户 Chrome 复制 profile）失败只作本源错误，
        # 不得向上抛炸穿整批抓取（历史上曾丢弃同批已抓好的全部源）
        session.start()
        return _run_fetch_once(session, source, limit=limit, timeout_ms=timeout_ms)
    except Exception as exc:
        return result_error(source, f"蓝宝书抓取失败: {exc}")
    finally:
        if own_session:
            session.shutdown()


def fetch_and_save_alphapai(source: dict, *, limit: int = 12, timeout_ms: int = 120000) -> tuple[FeedFetchResult, int]:
    result = fetch_alphapai_source(source, limit=limit, timeout_ms=timeout_ms)
    inserted = save_entries(result.entries) if result.ok else 0
    return result, inserted

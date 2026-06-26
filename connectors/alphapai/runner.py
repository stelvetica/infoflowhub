from __future__ import annotations

from pathlib import Path

from apps.subscriptions.models import FeedFetchResult
from apps.subscriptions.rss_db import save_entries
from connectors._shared.chrome_runner import SharedRunnerSession
from connectors._shared.common import CHROME_USER_DATA, USER_AGENT, result_error
from connectors.alphapai.feed import fetch_alphapai_with_page

DEBUG_DIR = Path(__file__).resolve().parents[2] / "runtime" / "debug"


def _write_debug(name: str, content: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / name).write_text(content, encoding="utf-8")


def _needs_profile_rebuild(result: FeedFetchResult) -> bool:
    error_text = str(result.error or "")
    if result.status == 401:
        return True
    return "登录态失效" in error_text or "登录" in error_text


def _run_fetch_once(session: SharedRunnerSession, source: dict, *, limit: int, timeout_ms: int) -> FeedFetchResult:
    from connectors.alphapai.browser import ALPHAPAI_TARGET_URL

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
    session: SharedRunnerSession | None = None,
) -> FeedFetchResult:
    own_session = session is None
    if own_session:
        from connectors.auth.providers.browser_profiles import AUTH_PROFILE_DIR
        session = SharedRunnerSession(
            source_profile_dir=AUTH_PROFILE_DIR,
            extra_args=[f"--user-agent={USER_AGENT}", "--lang=zh-CN,zh;q=0.9,en;q=0.8"],
        )
        session.start()
    try:
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

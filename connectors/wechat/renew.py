from __future__ import annotations

import asyncio
import threading

from connectors.auth.providers.wechat import get_wechat_status, log_wechat_auth_event
from web.services.wechat_login import renew_login_with_existing_credentials


WECHAT_AUTO_RENEW_WINDOW_MS = 3 * 24 * 3600 * 1000


def _run_async_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error["value"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


def should_auto_renew_wechat_auth() -> bool:
    status = get_wechat_status()
    expire_time = int(status.get("expire_time") or 0)
    if not status.get("is_available") or status.get("is_expired") or expire_time <= 0:
        return False
    remaining_ms = expire_time - int(__import__("time").time() * 1000)
    return remaining_ms <= WECHAT_AUTO_RENEW_WINDOW_MS


def ensure_wechat_auth_fresh_for_fetch() -> None:
    if not should_auto_renew_wechat_auth():
        return
    log_wechat_auth_event("命中抓取前自动续期窗口，准备免扫码续期。")
    try:
        _run_async_sync(renew_login_with_existing_credentials())
    except Exception as exc:
        log_wechat_auth_event(f"抓取前自动续期失败，将继续尝试本次抓取：{exc}")

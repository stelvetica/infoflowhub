from __future__ import annotations

import asyncio

from connectors.auth.models import AuthDescriptor
from connectors.auth.providers import wechat as wechat_provider
from connectors.wechat import renew as wechat_renew
from web.services import views as settings_views
from web.services import wechat_login


def test_get_wechat_credentials_migrates_legacy_to_canonical(tmp_path, monkeypatch):
    canonical_path = tmp_path / "auth" / "wechat_mp_main.json"
    legacy_path = tmp_path / "wechat_auth.json"
    legacy_payload = {
        "token": "legacy-token",
        "cookie": "legacy-cookie",
        "fakeid": "legacy-fakeid",
        "nickname": "旧账号",
        "expire_time": "1234567890",
    }
    wechat_provider.dump_json_utf8(legacy_path, legacy_payload)
    monkeypatch.setattr(wechat_provider, "WECHAT_AUTH_PATH", canonical_path)
    monkeypatch.setattr(wechat_provider, "LEGACY_WECHAT_AUTH_PATH", legacy_path)
    monkeypatch.setattr(wechat_provider, "AUTH_DIR", canonical_path.parent)

    credentials = wechat_provider.get_wechat_credentials()

    assert credentials["token"] == "legacy-token"
    assert credentials["cookie"] == "legacy-cookie"
    assert canonical_path.exists()
    assert wechat_provider.load_json_utf8(canonical_path)["token"] == "legacy-token"
    assert wechat_provider.load_json_utf8(legacy_path)["token"] == "legacy-token"


def test_save_wechat_credentials_only_writes_canonical(tmp_path, monkeypatch):
    canonical_path = tmp_path / "auth" / "wechat_mp_main.json"
    legacy_path = tmp_path / "wechat_auth.json"
    monkeypatch.setattr(wechat_provider, "WECHAT_AUTH_PATH", canonical_path)
    monkeypatch.setattr(wechat_provider, "LEGACY_WECHAT_AUTH_PATH", legacy_path)
    monkeypatch.setattr(wechat_provider, "AUTH_DIR", canonical_path.parent)

    wechat_provider.save_wechat_credentials(
        {
            "token": "canonical-token",
            "cookie": "canonical-cookie",
            "fakeid": "canonical-fakeid",
            "nickname": "主账号",
            "expire_time": 9876543210,
        }
    )

    assert canonical_path.exists()
    assert not legacy_path.exists()
    assert wechat_provider.load_json_utf8(canonical_path)["token"] == "canonical-token"


def test_settings_view_keeps_wechat_auth_actions(monkeypatch):
    monkeypatch.setattr(settings_views, "migrate_legacy_source_ids", lambda: None)
    monkeypatch.setattr(settings_views, "load_status", lambda: {"last_inserted_entries": 0, "last_error": ""})
    monkeypatch.setattr(settings_views, "format_success_sources_text", lambda status: "1/1")
    monkeypatch.setattr(settings_views, "read_log_tail", lambda path: "")
    monkeypatch.setattr(settings_views, "get_laterhub_summary", lambda: {})
    monkeypatch.setattr(settings_views, "get_laterhub_source_stats", lambda: [])
    monkeypatch.setattr(settings_views, "list_source_stats", lambda: [])
    monkeypatch.setattr(
        settings_views,
        "list_auth_statuses",
        lambda: [
            AuthDescriptor(
                auth_key="wechat_mp_main",
                platform="wechat",
                auth_mode="cookie_session",
                storage_ref="runtime/auth/wechat_mp_main.json",
                renew_strategy="scan_qr",
                display_name="微信公众号主账号",
                description="用于公众号抓取",
                status_text="可用",
                status_level="ok",
                hint="",
                is_available=True,
                is_expired=False,
                is_expiring_soon=True,
                remaining_hours=12,
            )
        ],
    )
    monkeypatch.setattr(
        settings_views,
        "get_wechat_auth_status",
        lambda: {
            "is_available": True,
            "is_expired": False,
            "is_expiring_soon": True,
            "remaining_text": "12 小时",
            "expire_time_text": "2026-05-23 09:13",
            "nickname": "主账号",
        },
    )
    monkeypatch.setattr(settings_views, "normalize_sources", lambda: [])

    settings = settings_views.get_settings_view({})

    asset = settings["auth_assets"][0]
    assert asset["action_url"].startswith("/wechat-login")
    assert asset["renew_action_url"] == "/actions/wechat-login/renew?view=settings"
    assert asset["renew_action_label"] == "免扫码续期"
    assert asset["nickname"] == "主账号"


def test_auto_renew_uses_canonical_credentials(monkeypatch):
    monkeypatch.setattr(
        wechat_renew,
        "get_wechat_status",
        lambda: {
            "is_available": True,
            "is_expired": False,
            "expire_time": 1,
        },
    )
    called = {"renewed": False}
    monkeypatch.setattr(
        wechat_renew,
        "_run_async_sync",
        lambda coro: (coro.close(), called.__setitem__("renewed", True)),
    )

    wechat_renew.ensure_wechat_auth_fresh_for_fetch()

    assert called["renewed"] is True


def test_manual_renew_saves_back_to_canonical(monkeypatch):
    monkeypatch.setattr(
        wechat_login,
        "get_wechat_credentials",
        lambda: {
            "token": "token-1",
            "cookie": "cookie-1",
            "fakeid": "fakeid-1",
            "nickname": "旧昵称",
            "expire_time": "1000",
        },
    )
    monkeypatch.setattr(
        wechat_login,
        "proxy_wechat_request",
        lambda **kwargs: asyncio.sleep(0, result=type("Resp", (), {"url": "https://mp.weixin.qq.com/cgi-bin/home", "text": "ok", "cookies": type("Cookies", (), {"jar": []})(), "headers": type("Headers", (), {"get_list": lambda self, key: ["sid=abc; Expires=Wed, 27 May 2026 09:13:00 GMT; Path=/"]})()})()),
    )
    monkeypatch.setattr(
        wechat_login,
        "_probe_api_session",
        lambda **kwargs: asyncio.sleep(0, result=(True, "cookie-2", ["sid=abc; Expires=Wed, 27 May 2026 09:13:00 GMT; Path=/"], {"ok": True})),
    )
    monkeypatch.setattr(
        wechat_login,
        "fetch_account_identity",
        lambda **kwargs: asyncio.sleep(0, result=("新昵称", "fakeid-2")),
    )
    saved: dict[str, object] = {}
    monkeypatch.setattr(wechat_login, "save_wechat_credentials", lambda payload: saved.update(payload))

    renewed, _ = asyncio.run(wechat_login.renew_login_with_existing_credentials())

    assert renewed["cookie"] == "cookie-2"
    assert saved["token"] == "token-1"
    assert saved["nickname"] == "新昵称"

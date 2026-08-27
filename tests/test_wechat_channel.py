"""微信公众号唯一通道（外部工具微信读书轻量接口）最小自检。

旧公众号后台通道已删除：api.py/renew.py/wechat_login.py/providers/wechat.py，
残留调用应显式失败；weread:// 源与认证资产透传是本仓侧仅存的微信逻辑。
"""

from __future__ import annotations

import pytest


def test_old_channel_modules_removed():
    with pytest.raises(ModuleNotFoundError):
        import connectors.wechat.api  # noqa: F401
    with pytest.raises(ModuleNotFoundError):
        import web.services.wechat_login  # noqa: F401
    with pytest.raises(ModuleNotFoundError):
        import connectors.auth.providers.wechat  # noqa: F401


def test_fetch_wechat_feed_rejects_non_weread_source():
    from connectors.wechat import fetch_wechat_feed

    result = fetch_wechat_feed(
        {"id": "wechat-x", "name": "旧协议源", "feed_url": "wechat://mp/fake123", "site_url": ""},
        limit=5,
    )
    assert not result.ok
    assert "weread://" in result.error


def test_registry_wechat_auth_reflects_external_tool_status(monkeypatch):
    from connectors.auth.registry import validate_auth

    monkeypatch.setattr(
        "web.services.weread_login.get_login_status",
        lambda **kwargs: {
            "is_available": False,
            "is_expired": True,
            "is_expiring_soon": False,
            "remaining_text": "",
            "expire_time_text": "",
            "nickname": "",
            "status_text": "外部工具未运行",
            "logged_in": False,
            "message": "外部工具未运行",
        },
    )

    descriptor = validate_auth("wechat_mp_main")

    assert descriptor.platform == "wechat"
    assert descriptor.is_available is False
    assert descriptor.is_expired is True

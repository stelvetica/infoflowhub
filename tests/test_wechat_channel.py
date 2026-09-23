"""微信/weread 抓取管线退役后的最小自检。

退役事实：抓取链（connectors/wechat）、认证项（wechat_mp_main）、
扫码登录页与路由均已删除；源配置不含 weread:// 源。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.subscriptions import rss_db
from web.app import app
from web.services import views


def test_wechat_runtime_modules_removed():
    # 按源码文件是否存在判定：删掉的包目录若只剩 __pycache__，Python 会把它当
    # 命名空间包导入成功（__file__ 为 None），用 import 断言会因文件系统残留而误判。
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "connectors/wechat/__init__.py",
        "connectors/wechat/feed.py",
        "web/services/weread_login.py",
        "web/services/wechat_login.py",
        "web/templates/wechat_login.html",
        "connectors/auth/providers/wechat.py",
    ):
        assert not (root / rel).exists(), f"{rel} 应已随微信管线退役删除"


def test_auth_registry_has_no_wechat_asset():
    from connectors.auth.registry import AUTH_REGISTRY

    assert "wechat_mp_main" not in AUTH_REGISTRY
    assert all(reg.platform != "wechat" for reg in AUTH_REGISTRY.values())


def test_source_config_has_no_weread_sources():
    from apps.subscriptions.rss_config import load_sources

    sources = load_sources()
    for source in sources:
        assert "weread://" not in str(source.get("feed_url") or "")
        assert "weread://" not in str(source.get("site_url") or "")
        assert source.get("channel") != "wechat"
        assert source.get("auth_key") != "wechat_mp_main"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", tmp_path / "subscriptions.sqlite3")
    monkeypatch.setattr(views, "REPORTS_DIR", tmp_path / "data" / "reports")
    monkeypatch.setattr(views, "migrate_legacy_source_ids", lambda: 0)
    return TestClient(app)


def test_legacy_wechat_routes_not_registered(client: TestClient):
    assert client.get("/wechat-login").status_code == 404
    assert client.get("/actions/weread-login/status").status_code == 404
    assert client.post("/actions/weread-login/start").status_code == 404
    assert client.post("/actions/weread-login/cancel").status_code == 404


def test_settings_view_renders_without_external_tool(client: TestClient):
    response = client.get("/?view=settings")

    assert response.status_code == 200
    assert "微信续期" not in response.text
    assert "wechat_mp_main" not in response.text


def test_main_view_has_no_wechat_renew_button(client: TestClient):
    response = client.get("/")

    assert response.status_code == 200
    assert "微信续期" not in response.text
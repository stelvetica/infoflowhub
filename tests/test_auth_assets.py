"""设置页认证资产语义 + 渠道列显示的回归测试。

只渲染设置页 HTML，不连真实 daemon、不起浏览器：bsk 命令解析入口被换成记录器
（BskBrowser 构造即调用 resolve_bsk_command），任何会话创建都会留痕。
认证状态检查全部打桩，不读本机 Chrome；validate_alphapai_auth 单独用临时 profile 测。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.subscriptions import rss_db
from connectors._shared.bsk_browser import BskError
from connectors.auth import registry
from connectors.auth.providers import browser_profiles
from web.app import app
from web.services import utils as web_utils
from web.services import views


ALPHAPAI_URL = "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"
SOCIAL_URL = "https://alphapai-web.rabyte.cn/reading/social-media/index"
# 设置页不应出现的字样（微信系退役、内部名、bsk 凭据类表述）。
FORBIDDEN_TEXT = ("微信", "alphapai_social", "ALPHAPAI_SOCIAL", "复制 profile", "bsk token")


@pytest.fixture()
def bsk_invocations(tmp_path, monkeypatch):
    """bsk 调用哨兵：resolve_bsk_command 一被调用就记一笔（会话构造即调用）。"""
    log = tmp_path / "bsk.log"

    def fake_resolve():
        existing = log.read_text(encoding="utf-8") if log.exists() else ""
        log.write_text(existing + "resolve\n", encoding="utf-8")
        return (sys.executable, "-c", "raise SystemExit(1)")

    monkeypatch.setattr("connectors._shared.bsk_browser.resolve_bsk_command", fake_resolve)

    def calls() -> list[str]:
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    return calls


def _stub_auth_validators(monkeypatch) -> None:
    """把 7 个认证校验全打桩为「可用」，避免测试依赖真实 Chrome / cookie。"""
    ok = {"is_available": True, "status_level": "ok", "status_text": "可用", "hint": ""}
    for name in (
        "validate_alphapai_auth",
        "validate_douyin_auth",
        "validate_x_auth",
        "validate_xiaoheihe_auth",
        "validate_youtube_auth",
        "validate_jiuyangongshe_auth",
        "validate_bilibili_auth",
    ):
        monkeypatch.setattr(registry, name, lambda: dict(ok))
    registry.invalidate_auth_status_cache()


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", tmp_path / "subscriptions.sqlite3")
    monkeypatch.setattr(views, "REPORTS_DIR", tmp_path / "data" / "reports")
    monkeypatch.setattr(views, "migrate_legacy_source_ids", lambda: 0)
    _stub_auth_validators(monkeypatch)
    # 不用 with 进入上下文，避免触发 lifespan（不起 daemon）。
    yield TestClient(app)
    # 清掉打桩期间写入的 60s 状态缓存，避免污染后续用真实校验的测试。
    registry.invalidate_auth_status_cache()


def test_settings_page_renders_alpha_auth_asset(client: TestClient, bsk_invocations) -> None:
    response = client.get("/?view=settings")

    assert response.status_code == 200
    html = response.text
    # 认证资产里只有一个 Alpha派 条目（面板用 <strong> 渲染展示名）。
    assert html.count("<strong>Alpha派</strong>") == 1
    assert "日常 Chrome 登录会话" in html
    assert "复用本机 Chrome 会话，不导出 token" in html
    assert "请在日常 Chrome 登录 Alpha派" in html
    # 渲染认证资产/源表全程不创建任何 bsk 会话。
    assert bsk_invocations() == []


def test_settings_page_has_no_forbidden_wording(client: TestClient) -> None:
    html = client.get("/?view=settings").text

    for text in FORBIDDEN_TEXT:
        assert text not in html


def test_both_alphapai_sources_depend_on_alpha_auth(client: TestClient) -> None:
    html = client.get("/?view=settings").text

    assert "资讯 Alpha派蓝宝书" in html
    assert "资讯 Alpha派公众号" in html
    # 依赖认证资产列（requirement-badge）两条都指向 Alpha派。
    assert html.count('<span class="requirement-badge">Alpha派</span>') >= 2


def test_channel_column_shows_localized_labels(client: TestClient) -> None:
    html = client.get("/?view=settings").text

    assert "Alpha派公众号" in html
    assert "alphapai_social" not in html
    assert "ALPHAPAI_SOCIAL" not in html
    # 蓝宝书那条也走中文标签，不显示内部名 ALPHAPAI。
    assert web_utils.source_channel_label(ALPHAPAI_URL, ALPHAPAI_URL, "web") == "Alpha派"
    assert web_utils.source_channel_label(SOCIAL_URL, SOCIAL_URL, "web") == "Alpha派公众号"


def test_settings_page_ok_when_bsk_daemon_unavailable(client: TestClient, monkeypatch) -> None:
    def _missing():
        raise BskError("未找到 bsk CLI，请先安装 browser-skill（bsk）")

    monkeypatch.setattr("connectors._shared.bsk_browser.resolve_bsk_command", _missing)

    response = client.get("/?view=settings")

    assert response.status_code == 200


# ---- validate_alphapai_auth：只给非敏感可读状态 ----


def _make_cookie_db(path: Path, *, with_rabyte: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    if with_rabyte:
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".rabyte.cn", "SESSION", "SUPER_SECRET_COOKIE_VALUE"),
        )
    conn.commit()
    conn.close()


def test_alphapai_status_never_leaks_cookie_values(tmp_path, monkeypatch) -> None:
    profile = tmp_path / "Default"
    _make_cookie_db(profile / "Network" / "Cookies", with_rabyte=True)
    monkeypatch.setattr(browser_profiles, "_default_chrome_profile_dir", lambda: profile)

    status = browser_profiles.validate_alphapai_auth()

    assert status["is_available"] is True
    assert status["status_text"] == "可用"
    assert "SUPER_SECRET_COOKIE_VALUE" not in json.dumps(status, ensure_ascii=False)
    assert "SESSION" not in json.dumps(status, ensure_ascii=False)


def test_alphapai_status_is_readable_when_not_logged_in(tmp_path, monkeypatch) -> None:
    profile = tmp_path / "Default"
    _make_cookie_db(profile / "Network" / "Cookies", with_rabyte=False)
    monkeypatch.setattr(browser_profiles, "_default_chrome_profile_dir", lambda: profile)

    status = browser_profiles.validate_alphapai_auth()

    assert status["is_available"] is False
    assert status["status_text"] == "需在 Chrome 登录"
    assert status["hint"] == "请在日常 Chrome 登录 Alpha派。"


def test_alphapai_status_readable_when_cookie_db_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(browser_profiles, "_default_chrome_profile_dir", lambda: tmp_path / "Default")

    status = browser_profiles.validate_alphapai_auth()

    assert status["status_text"] == "需在 Chrome 登录"
    assert "微信" not in json.dumps(status, ensure_ascii=False)
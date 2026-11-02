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
FORBIDDEN_TEXT = ("微信公众号", "微信读书扫码", "wechat_mp", "alphapai_social", "ALPHAPAI_SOCIAL", "复制 profile", "bsk token")

# 走 bsk 后端的认证资产：登录方式统一为「日常 Chrome 登录会话」。
BSK_AUTH_KEYS = (
    "douyin_shared",
    "x_profile2",
    "xiaoheihe_shared",
    "youtube_main",
    "alphapai_main",
)
# 旧「复制 profile」机制的文案，走 bsk 的站点不得再出现。
COPY_PROFILE_WORDING = ("复制常用 Chrome Default", "复制常用Chrome登录态", "抓取时复制")


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


def test_settings_page_shows_laterhub_source_management(client: TestClient) -> None:
    html = client.get("/?view=settings").text
    assert "稍后处理源管理" in html
    assert "/actions/laterhub-source/weread_shelf/toggle-enabled" in html


def test_both_alphapai_sources_depend_on_alpha_auth(client: TestClient) -> None:
    html = client.get("/?view=settings").text

    # 按源 id 判定（渲染在操作 URL 里），不用展示名——展示名用户随时可改。
    assert "/actions/source/alphapai/toggle-enabled" in html
    assert "/actions/source/alphapai-social-media/toggle-enabled" in html
    # 依赖认证资产列（requirement-badge）两条都指向 Alpha派。
    assert html.count('<span class="requirement-badge">Alpha派</span>') >= 2


def test_infer_source_meta_treats_recognized_sites_as_web() -> None:
    """浏览器抓取的源必须判为 web：判成 native 会在保存时掉进 RSS 路径，抓不到内容。"""
    for url in (
        SOCIAL_URL,
        ALPHAPAI_URL,
        "https://space.bilibili.com/123/dynamic",
        "https://www.jiuyangongshe.com/u/abc",
    ):
        assert views.infer_source_meta(url, url) == ("web", "web", "web"), url
    # 纯 RSS 源仍归 native，rsshub 源仍归 rsshub。
    assert views.infer_source_meta("https://example.com/feed.xml", "https://example.com") == ("native", "direct", "native")
    assert views.infer_source_meta("https://rsshub.app/x/user/abc", "") == ("rsshub", "rsshub-self-hosted", "rsshub")


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


# ---- 认证资产文案：走 bsk 的站点统一口径，不再描述旧「复制 profile」机制 ----


def test_bsk_auth_assets_use_daily_chrome_wording() -> None:
    """走 bsk 的站点登录方式统一，且文案里不再出现旧复制机制字样。"""
    for key in BSK_AUTH_KEYS:
        reg = registry.resolve_auth(key)
        assert reg.login_method == "日常 Chrome 登录会话", key
        blob = " ".join((reg.description, reg.login_method, reg.storage_ref, reg.renew_label))
        for bad in COPY_PROFILE_WORDING:
            assert bad not in blob, (key, bad)


def test_bsk_site_status_hints_do_not_mention_copying(tmp_path, monkeypatch) -> None:
    """browser_profiles 的状态 hint 也渲染在同一行说明里，不得再提复制机制。"""
    monkeypatch.setattr(browser_profiles, "_default_chrome_profile_dir", lambda: tmp_path / "Default")
    for status in (
        browser_profiles.validate_douyin_auth(),
        browser_profiles.validate_x_auth(),
        browser_profiles.validate_xiaoheihe_auth(),
        browser_profiles.validate_youtube_auth(),
    ):
        for bad in COPY_PROFILE_WORDING:
            assert bad not in status["hint"], bad


def test_settings_page_auth_assets_drop_copy_profile_wording(client: TestClient) -> None:
    html = client.get("/?view=settings").text

    for bad in COPY_PROFILE_WORDING:
        assert bad not in html
    # 走 bsk 的 6 个站点，登录方式统一为「日常 Chrome 登录会话」。
    assert html.count("日常 Chrome 登录会话") >= len(BSK_AUTH_KEYS)
"""小黑盒主页源迁到 bsk 后端：$hbRequest 直取路径、探测式降级与配置一致性。

假 page 打桩，不连真实 daemon / Chrome。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.subscriptions import rss_config
from connectors._shared import web_fetch
from connectors._shared.bsk_session import BskRunnerSession
from connectors.xiaoheihe.profile import fetch_xiaoheihe_profile_with_page

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = {
    "id": "xiaoheihe_cuojuehuanjue",
    "name": "技术 周报 错觉幻视",
    "feed_url": "https://www.xiaoheihe.cn/app/user/profile/51993878",
    "site_url": "https://www.xiaoheihe.cn/app/user/profile/51993878",
    "auth_key": "xiaoheihe_shared",
    "provider": "web",
    "fetch_via": "web",
}

MOMENT = {
    "linkid": 191208611,
    "title": "《最终幻想7：启示录》PS5 Pro 可实现4K/60帧",
    "description": "摘要A",
    "create_at": 1790155926,
}


class FakeBskPage:
    """bsk 形态：无 page.on，evaluate 直接返回 $hbRequest 路径的结果信封。"""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.goto_url = ""
        self.evaluated: list = []

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        return None

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluated.append((js, arg))
        return self.payload


class _FakeResponse:
    def __init__(self, url: str, payload) -> None:
        self.url = url
        self._payload = payload

    def json(self):
        return self._payload


class FakeCopyPage:
    """copy 形态：有 on/remove_listener，goto 时把响应喂给拦截回调。"""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.handlers: dict = {}
        self.listened: list[str] = []
        self.removed: list[str] = []

    def on(self, event, cb) -> None:
        self.handlers[event] = cb
        self.listened.append(event)

    def remove_listener(self, event, cb) -> None:
        self.removed.append(event)

    def goto(self, url, wait_until=None, timeout=None) -> None:
        cb = self.handlers.get("response")
        if cb:
            cb(_FakeResponse("https://api.xiaoheihe.cn/bbs/app/profile/events?userid=51993878", self.payload))

    def wait_for_timeout(self, ms) -> None:
        return None


def test_bsk_page_uses_hbrequest_and_parses_entries():
    page = FakeBskPage({"status": "ok", "msg": "", "moments": [dict(MOMENT)]})

    result = fetch_xiaoheihe_profile_with_page(page, SOURCE)

    assert result.ok and len(result.entries) == 1
    entry = result.entries[0]
    assert entry.link == "https://www.xiaoheihe.cn/app/bbs/link/191208611"
    assert entry.title == MOMENT["title"]
    # 走的是 $hbRequest 路径，没有 page.on 拦截。
    assert page.evaluated and "/bbs/app/profile/events" in page.evaluated[0][1]


def test_bsk_page_error_envelope_becomes_failure():
    page = FakeBskPage({"error": "未能获取小黑盒接口签名，前端封装可能已变更"})

    result = fetch_xiaoheihe_profile_with_page(page, SOURCE)

    assert not result.ok
    assert "前端封装可能已变更" in result.error


def test_bsk_page_empty_moments_hints_login():
    page = FakeBskPage({"status": "ok", "msg": "", "moments": []})

    result = fetch_xiaoheihe_profile_with_page(page, SOURCE)

    assert not result.ok
    assert "未登录" in result.error


def test_copy_page_still_uses_response_interception():
    """copy 后端（page 有 on）保持原有拦截行为，不坏最后手段。"""
    payload = {"result": {"moments": [dict(MOMENT)]}}
    page = FakeCopyPage(payload)

    result = fetch_xiaoheihe_profile_with_page(page, SOURCE)

    assert result.ok and len(result.entries) == 1
    assert page.listened == ["response"] and page.removed == ["response"]


# ---- 分派 ----

@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "rss_settings.json"

    def write(section: dict | None) -> None:
        path.write_text(json.dumps({"browser_backend": section} if section is not None else {}), encoding="utf-8")

    monkeypatch.setattr(rss_config, "SETTINGS_PATH", path)
    return write


def test_xiaoheihe_source_routes_to_bsk_session(settings_file):
    settings_file({"default": "copy", "sources": {"xiaoheihe_cuojuehuanjue": "bsk"}})

    assert web_fetch._uses_bsk(SOURCE) is True
    session = web_fetch._make_session_for(SOURCE)
    assert isinstance(session, BskRunnerSession)
    # 构造期不得起 bsk 子进程（懒启动）。
    assert session.browser.session_id is None


# ---- 配置一致性：auth_key 拼写 ----

def test_source_auth_key_matches_registry():
    from connectors.auth.registry import AUTH_REGISTRY

    sources = json.loads((REPO_ROOT / "config" / "subscription_sources.json").read_text(encoding="utf-8"))["sources"]
    entry = next(s for s in sources if s["id"] == "xiaoheihe_cuojuehuanjue")

    assert entry["auth_key"] == "xiaoheihe_shared"
    assert entry["auth_key"] in AUTH_REGISTRY


def test_xiaoheihe_source_configured_for_bsk_backend():
    settings = json.loads((REPO_ROOT / "config" / "rss_settings.json").read_text(encoding="utf-8"))
    assert settings["browser_backend"]["sources"]["xiaoheihe_cuojuehuanjue"] == "bsk"
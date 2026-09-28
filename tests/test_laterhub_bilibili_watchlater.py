"""laterhub「B 站稍后看」= 官方 API 直连（HTTP + .env cookie）。

它不进 bsk：不拉浏览器、不需要 session，bsk 会话起不来时也能抓。本文件把这条口径钉住。
不触真实网络：requests.Session 被替换成假实现。
"""

from __future__ import annotations

import pytest

from connectors.bilibili import api as bili


class FakeResponse:
    def __init__(self, payload, status_code=200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeRequestsSession:
    """记录请求；按 URL 返回预设载荷（未预设则用 default）。"""

    def __init__(self, responses: dict[str, FakeResponse] | None = None, default: FakeResponse | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.trust_env = True
        self.responses = responses or {}
        self.default = default
        self.get_calls: list[tuple[str, dict, int]] = []

    def get(self, url, params=None, timeout=None):
        self.get_calls.append((url, dict(params or {}), timeout))
        response = self.responses.get(url, self.default)
        if response is None:
            raise AssertionError(f"测试未预设该 URL 的响应: {url}")
        return response


@pytest.fixture
def wired(monkeypatch):
    """把 requests.Session 换成假实现，并让 BilibiliConfig 无需 .env。"""

    def _wire(responses=None, default=None):
        fake = FakeRequestsSession(responses=responses, default=default)
        monkeypatch.setattr(bili.requests, "Session", lambda: fake)
        monkeypatch.setattr(
            bili.BilibiliConfig,
            "from_env",
            classmethod(lambda cls, env_path=None: cls(cookie="SESSDATA=test")),
        )
        return fake

    return _wire


def _ok_payload(*items: dict) -> dict:
    return {"code": 0, "message": "0", "data": {"list": list(items)}}


SAMPLE_ITEM = {"bvid": "BV1xx411c7mD", "title": "投资思路分享", "owner": {"name": "Br7uce"}, "aid": 12345}


def test_uses_official_api_with_ps_and_watchlater_referer(wired):
    fake = wired(default=FakeResponse(_ok_payload(SAMPLE_ITEM)))

    items = bili.fetch_bilibili_watchlater("ignored.env")

    assert items == [
        {
            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
            "title": "投资思路分享",
            "source": "bilibili_watchlater",
            "owner": "Br7uce",
            "tags": None,
            "raw": SAMPLE_ITEM,
        }
    ]
    url, params, timeout = fake.get_calls[0]
    assert url == bili.BILIBILI_API_CANDIDATES[0]
    assert params == {"ps": 20}
    assert timeout == 20
    assert fake.headers["Referer"] == "https://www.bilibili.com/watchlater/list"
    assert fake.headers["Cookie"] == "SESSDATA=test"
    # 国内站点绕过系统代理
    assert fake.trust_env is False


def test_falls_back_to_second_api_candidate(wired):
    """首选接口不可用时按候选顺序退到第二个（迁移前既有行为）。"""
    first, second = bili.BILIBILI_API_CANDIDATES
    fake = wired(
        responses={
            first: FakeResponse({"code": -101, "message": "账号未登录"}),
            second: FakeResponse(_ok_payload(SAMPLE_ITEM)),
        }
    )

    items = bili.fetch_bilibili_watchlater()

    assert len(items) == 1
    assert [call[0] for call in fake.get_calls] == [first, second]


def test_all_candidates_failed_raises_readable_error(wired):
    fake = wired(default=FakeResponse({"code": -101, "message": "账号未登录"}))

    with pytest.raises(RuntimeError) as excinfo:
        bili.fetch_bilibili_watchlater()

    assert "获取 B 站稍后看失败" in str(excinfo.value)
    assert len(fake.get_calls) == len(bili.BILIBILI_API_CANDIDATES)


def test_non_json_response_raises_readable_error(wired):
    wired(default=FakeResponse(ValueError("Expecting value: line 1 column 1")))

    with pytest.raises(RuntimeError) as excinfo:
        bili.fetch_bilibili_watchlater()

    assert "获取 B 站稍后看失败" in str(excinfo.value)


def test_unrecognized_structure_raises_readable_error(wired):
    wired(default=FakeResponse({"code": 0, "data": {"unexpected": 1}}))

    with pytest.raises(RuntimeError) as excinfo:
        bili.fetch_bilibili_watchlater()

    assert "无法识别" in str(excinfo.value)


def test_empty_watchlater_returns_empty_list_not_error(wired):
    """用户把稍后看清空是合法结果：返回 []，可见性由 pipeline 的「0 条」WARN 承担。"""
    wired(default=FakeResponse(_ok_payload()))

    assert bili.fetch_bilibili_watchlater() == []


def test_missing_cookie_fails_loudly(monkeypatch):
    """没有 .env 登录态时必须明确报错，而不是静默返回空列表。"""
    monkeypatch.setattr(bili, "get_bilibili_cookie", lambda: "")

    with pytest.raises(ValueError) as excinfo:
        bili.BilibiliConfig.from_env()

    assert "B 站登录态" in str(excinfo.value)

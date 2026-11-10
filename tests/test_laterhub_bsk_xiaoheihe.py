"""laterhub「小黑盒收藏」抓取器迁到 bsk 后端：页内 $hbRequest 取数、会话契约与静态守卫。

假 page 打桩（刻意不提供 on 方法），不连真实 daemon / Chrome。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from connectors.xiaoheihe import feed

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeBskPage:
    """bsk 形态假 page：刻意不提供 on / remove_listener，结构性证明不再依赖网络拦截。"""

    def __init__(self, *, login_ok: bool = True, favour_payload=None, goto_error=None) -> None:
        self.login_ok = login_ok
        self.favour_payload = (
            favour_payload
            if favour_payload is not None
            else {"total_page": 1, "count": 0, "items": []}
        )
        self.goto_error = goto_error
        self.goto_url = ""
        self.evaluated: list = []

    def goto(self, url, wait_until=None, timeout=None) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        return None

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluated.append((js, arg))
        if "restore_login" in js:
            return self.login_ok
        return self.favour_payload

    def close(self) -> None:
        return None


class FakeExternalSession:
    """外部传入的会话：只使用，绝不应被 start / shutdown。"""

    def __init__(self, page: FakeBskPage) -> None:
        self.page = page
        self.started = 0
        self.shutdowns = 0

    def start(self) -> None:
        self.started += 1

    def shutdown(self) -> None:
        self.shutdowns += 1

    def acquire_page(self) -> FakeBskPage:
        return self.page


@pytest.fixture(autouse=True)
def _isolate_debug(tmp_path, monkeypatch):
    monkeypatch.setattr(feed, "DEBUG_JSON_PATH", tmp_path / "dbg.json")


def test_normal_path_uses_page_evaluate_and_does_not_touch_external_session():
    page = FakeBskPage(
        favour_payload={
            "total_page": 1,
            "count": 2,
            "items": [
                {"linkid": 111, "title": "标题 A"},
                {"linkid": 222, "title": ""},
            ],
        }
    )
    session = FakeExternalSession(page)

    batch = feed.fetch_xiaoheihe_favorites(session=session, deep=True)
    result = batch.items

    assert [r["url"] for r in result] == [
        "https://www.xiaoheihe.cn/app/bbs/link/111",
        "https://www.xiaoheihe.cn/app/bbs/link/222",
    ]
    assert all(r["source"] == "xiaoheihe_favor" for r in result)
    assert result[1]["title"] == "小黑盒帖子_222"
    # 未截断 = 拿到完整收藏列表，可按「源内已移除」比对。
    assert batch.complete is True
    # 外部会话只使用：不 start / 不 shutdown。
    assert session.started == 0 and session.shutdowns == 0
    # 假 page 无 on，结构上不可能走网络拦截。
    assert not hasattr(page, "on")
    assert any(arg and "/bbs/app/favour/list" in arg for _, arg in page.evaluated)


def test_truncated_fetch_reports_incomplete_batch():
    """撞翻页上限（truncated）时只拿到部分数据：complete 必须为 False，禁止据此判定条目已移除。"""
    page = FakeBskPage(
        favour_payload={
            "total_page": 5,
            "pages_fetched": 50,
            "truncated": True,
            "count": 1,
            "items": [{"linkid": 333, "title": "标题 C"}],
        }
    )
    session = FakeExternalSession(page)

    batch = feed.fetch_xiaoheihe_favorites(session=session, deep=True)

    assert [r["url"] for r in batch.items] == ["https://www.xiaoheihe.cn/app/bbs/link/333"]
    assert batch.complete is False


def test_self_built_session_starts_and_shuts_down_once(monkeypatch):
    created: list = []

    class FakeBskSession:
        def __init__(self, *, start_url="about:blank") -> None:
            self.start_url = start_url
            self.started = 0
            self.shutdowns = 0
            self.page = FakeBskPage(
                favour_payload={"total_page": 1, "count": 1, "items": [{"linkid": 999, "title": "T"}]}
            )
            created.append(self)

        def start(self) -> None:
            self.started += 1

        def shutdown(self) -> None:
            self.shutdowns += 1

        def acquire_page(self) -> FakeBskPage:
            return self.page

    monkeypatch.setattr(feed, "BskRunnerSession", FakeBskSession)

    batch = feed.fetch_xiaoheihe_favorites(deep=True)

    assert len(created) == 1
    assert created[0].start_url == "about:blank"
    assert created[0].started == 1 and created[0].shutdowns == 1
    assert batch.items[0]["url"].endswith("/999")
    assert batch.complete is True


def test_self_built_session_shuts_down_on_exception(monkeypatch):
    created: list = []

    class FakeBskSession:
        def __init__(self, *, start_url="about:blank") -> None:
            self.started = 0
            self.shutdowns = 0
            self.page = FakeBskPage(goto_error=RuntimeError("boom"))
            created.append(self)

        def start(self) -> None:
            self.started += 1

        def shutdown(self) -> None:
            self.shutdowns += 1

        def acquire_page(self) -> FakeBskPage:
            return self.page

    monkeypatch.setattr(feed, "BskRunnerSession", FakeBskSession)

    with pytest.raises(RuntimeError, match="导航小黑盒首页失败"):
        feed.fetch_xiaoheihe_favorites(deep=True)

    assert created[0].started == 1 and created[0].shutdowns == 1


def test_login_failure_raises_with_login_hint():
    session = FakeExternalSession(FakeBskPage(login_ok=False))

    with pytest.raises(RuntimeError) as excinfo:
        feed.fetch_xiaoheihe_favorites(session=session, deep=True)

    message = str(excinfo.value)
    assert "登录" in message
    assert "日常 Chrome" in message
    assert session.shutdowns == 0


def test_missing_signature_raises():
    page = FakeBskPage(
        favour_payload={"error": "未能获取小黑盒收藏接口签名（可能未登录或前端封装已变更）"}
    )
    session = FakeExternalSession(page)

    with pytest.raises(RuntimeError) as excinfo:
        feed.fetch_xiaoheihe_favorites(session=session, deep=True)

    message = str(excinfo.value)
    assert "签名" in message
    assert "登录" in message


def test_feed_source_has_no_legacy_backend_tokens():
    source = (REPO_ROOT / "connectors" / "xiaoheihe" / "feed.py").read_text(encoding="utf-8")
    for token in ("CopyRunnerSession", "LATERHUB_RUNNER_DIR", "9225", "page.on("):
        assert token not in source, f"feed.py 仍残留旧后端痕迹: {token}"

"""laterhub「B 站稍后看」= bsk 页内 fetch（与订阅动态同一姿势）。

本文件把「稍后看不读 cookie 文件、不需要手动同步登录态」钉住，并把翻全量的
批次契约（items + complete）钉住。
不触真实网络/浏览器：page facade 与 bsk 会话均用假实现。
"""

from __future__ import annotations

import json

import pytest

from connectors.bilibili import api as bili


class FakePage:
    def __init__(self, payload=..., payload_error: Exception | None = None) -> None:
        self.payload = payload
        self.payload_error = payload_error
        self.gotos: list[str] = []
        self.evaluate_calls: list[tuple] = []

    def goto(self, url, wait_until=None):
        self.gotos.append(url)

    def evaluate(self, js, arg=None, timeout=None):
        self.evaluate_calls.append((js, arg, timeout))
        if self.payload_error is not None:
            raise self.payload_error
        return self.payload


class FakeBskSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.shutdown_calls = 0

    def acquire_page(self):
        return self.page

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()
        return False


def _batch_payload(*items: dict, complete: bool = True) -> dict:
    """页内 JS 归一后的批次载荷（原始 item 列表 + 是否拿到完整列表）。"""
    return {"items": list(items), "complete": complete}


SAMPLE_ITEM = {"bvid": "BV1xx411c7mD", "title": "投资思路分享", "owner": {"name": "Br7uce"}, "aid": 12345}


def _item(index: int) -> dict:
    return {"bvid": f"BV{index}", "title": f"视频 {index}", "owner": {"name": "UP"}}


def test_watchlater_uses_bsk_page_and_official_api():
    page = FakePage(_batch_payload(SAMPLE_ITEM))
    session = FakeBskSession(page)

    batch = bili.fetch_bilibili_watchlater("ignored.env", session=session, deep=True)

    assert batch.items == [
        {
            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
            "title": "投资思路分享",
            "source": "bilibili_watchlater",
            "owner": "Br7uce",
            "tags": None,
            "raw": SAMPLE_ITEM,
        }
    ]
    assert batch.complete is True
    # 先导航到 watchlater 页拿同域上下文/Referer
    assert page.gotos == [bili.BILIBILI_WATCHLATER_PAGE_URL]
    js, arg, timeout = page.evaluate_calls[0]
    assert "credentials" in js and "include" in js
    args = json.loads(arg)
    assert args["url"] == bili.BILIBILI_WATCHLATER_API
    assert args["ps"] == 20
    assert args["maxPages"] == bili.BILIBILI_WATCHLATER_MAX_PAGES
    assert timeout >= 120  # 页内 fetch 预算要大于 30s 看门狗
    assert session.shutdown_calls == 0  # 外部会话归调用方所有


def test_multi_page_fetch_collects_all_in_single_evaluate():
    """20/20/5 三页 → 45 条且 complete=True；翻页在页内完成，Python 侧仍只调一次 evaluate。"""
    items = [_item(index) for index in range(45)]
    page = FakePage(_batch_payload(*items))

    batch = bili.fetch_bilibili_watchlater(session=FakeBskSession(page), deep=True)

    assert len(batch.items) == 45
    assert batch.items[0]["url"] == "https://www.bilibili.com/video/BV0"
    assert batch.items[-1]["url"] == "https://www.bilibili.com/video/BV44"
    assert batch.complete is True
    assert len(page.evaluate_calls) == 1


def test_second_page_api_error_raises():
    page = FakePage({"code": -400, "message": "请求错误", "items": []})

    with pytest.raises(RuntimeError) as excinfo:
        bili.fetch_bilibili_watchlater(session=FakeBskSession(page), deep=True)

    assert "code=-400" in str(excinfo.value)


def test_hitting_max_pages_marks_batch_incomplete():
    items = [_item(index) for index in range(1000)]
    page = FakePage(_batch_payload(*items, complete=False))

    batch = bili.fetch_bilibili_watchlater(session=FakeBskSession(page), deep=True)

    assert len(batch.items) == 1000
    assert batch.complete is False


def test_login_expired_raises_readable_error():
    page = FakePage({"code": -101, "message": "账号未登录"})

    with pytest.raises(RuntimeError) as excinfo:
        bili.fetch_bilibili_watchlater(session=FakeBskSession(page), deep=True)

    assert "登录态失效" in str(excinfo.value)
    assert "-101" in str(excinfo.value)


def test_page_fetch_error_envelope_raises():
    page = FakePage({"error": "接口返回非 JSON（可能登录态失效）"})

    with pytest.raises(RuntimeError) as excinfo:
        bili.fetch_bilibili_watchlater(session=FakeBskSession(page), deep=True)

    assert "非 JSON" in str(excinfo.value)


def test_unrecognized_structure_raises_readable_error():
    page = FakePage({"unrecognized": True})

    with pytest.raises(RuntimeError) as excinfo:
        bili.fetch_bilibili_watchlater(session=FakeBskSession(page), deep=True)

    assert "无法识别" in str(excinfo.value)


def test_non_object_payload_raises():
    page = FakePage(["not", "an", "object"])

    with pytest.raises(RuntimeError) as excinfo:
        bili.fetch_bilibili_watchlater(session=FakeBskSession(page), deep=True)

    assert "非对象" in str(excinfo.value)


def test_empty_watchlater_returns_empty_complete_batch():
    """用户把稍后看清空是合法结果：返回空批次且 complete=True，可见性由 pipeline 的「0 条」WARN 承担。"""
    page = FakePage(_batch_payload())

    batch = bili.fetch_bilibili_watchlater(session=FakeBskSession(page), deep=True)

    assert batch.items == []
    assert batch.complete is True


def test_without_session_builds_and_closes_own_bsk_session(monkeypatch):
    page = FakePage(_batch_payload(SAMPLE_ITEM))
    session = FakeBskSession(page)
    import connectors._shared.bsk_session as bsk_session_mod

    monkeypatch.setattr(bsk_session_mod, "BskRunnerSession", lambda **kwargs: session)

    batch = bili.fetch_bilibili_watchlater(deep=True)

    assert len(batch.items) == 1
    assert session.shutdown_calls == 1
    assert page.gotos == [bili.BILIBILI_WATCHLATER_PAGE_URL]

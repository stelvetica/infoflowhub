"""laterhub「微信读书书架」抓取器：归一化、会话契约与失败可见性。

假 page 打桩（不连真实 daemon / Chrome）验证页内同源接口取数路径。
"""

from __future__ import annotations

import json

import pytest

from connectors.weread import shelf

READER = "https://weread.qq.com/web/reader/abc123"


class FakeBskPage:
    def __init__(self, *, payload=None, goto_error=None) -> None:
        self.payload = payload if payload is not None else {"ids": 0, "books": [], "badIds": []}
        self.goto_error = goto_error
        self.goto_url = ""
        self.evaluated: list = []
        self.closed = 0

    def goto(self, url, wait_until=None, timeout=None) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        return None

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluated.append((js, arg, kwargs))
        return self.payload

    def close(self) -> None:
        self.closed += 1


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
    monkeypatch.setattr(shelf, "DEBUG_JSON_PATH", tmp_path / "dbg.json")


def _book(**overrides):
    item = {
        "bookId": "b1",
        "readerLink": READER,
        "title": "  三体   全集  ",
        "finishReading": 0,
    }
    item.update(overrides)
    return item


def test_normalize_keeps_reader_link_and_compresses_title():
    item = _book()
    got = shelf.normalize_weread_item(item)
    assert got == {
        "url": READER,
        "title": "三体 全集",
        "source": "weread_shelf",
        "owner": "",
        "tags": None,
        "keep_finished": True,
        "raw": item,
    }


def test_normalize_keeps_finished_but_drops_empty_title_and_non_reader_link():
    # 不按「读完」过滤：书架里的书全部入库。
    assert shelf.normalize_weread_item(_book(finishReading=1)) is not None
    assert shelf.normalize_weread_item(_book(title="   \n\t ")) is None
    assert shelf.normalize_weread_item(_book(readerLink="https://weread.qq.com/web/bookDetail/x")) is None
    assert shelf.normalize_weread_item(_book(readerLink="")) is None


def test_save_items_forwards_keep_finished():
    """连接器 → pipeline → DB 的完成态豁免必须贯通，否则书每天复活。"""
    from apps.laterhub import pipeline

    captured: list = []

    class SpyDB:
        def upsert_link(self, record) -> None:
            captured.append(record)

    item = shelf.normalize_weread_item(_book())
    pipeline._save_items(SpyDB(), [item])

    assert captured[0].keep_finished is True


def test_external_session_is_only_used_and_navigates_to_shelf():
    page = FakeBskPage(payload={"ids": 1, "books": [_book()], "badIds": ["mpbook"]})
    session = FakeExternalSession(page)

    result = shelf.fetch_weread_shelf(session=session).items

    assert [r["url"] for r in result] == [READER]
    assert result[0]["source"] == "weread_shelf"
    assert page.goto_url == "https://weread.qq.com/web/shelf"
    assert session.started == 0 and session.shutdowns == 0
    assert page.closed == 1
    js, arg, kwargs = page.evaluated[0]
    assert arg == json.dumps({"batchSize": 20, "timeoutMs": 20000, "deep": False})
    assert kwargs == {"timeout": 120}


def test_self_built_session_starts_and_shuts_down_once(monkeypatch):
    created: list = []

    class FakeBskSession:
        def __init__(self, *, start_url="about:blank") -> None:
            self.start_url = start_url
            self.started = 0
            self.shutdowns = 0
            self.page = FakeBskPage(payload={"ids": 1, "books": [_book()], "badIds": []})
            created.append(self)

        def start(self) -> None:
            self.started += 1

        def shutdown(self) -> None:
            self.shutdowns += 1

        def acquire_page(self) -> FakeBskPage:
            return self.page

    monkeypatch.setattr(shelf, "BskRunnerSession", FakeBskSession)

    result = shelf.fetch_weread_shelf().items

    assert len(created) == 1
    assert created[0].start_url == "about:blank"
    assert created[0].started == 1 and created[0].shutdowns == 1
    assert result[0]["url"] == READER


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

    monkeypatch.setattr(shelf, "BskRunnerSession", FakeBskSession)

    with pytest.raises(RuntimeError):
        shelf.fetch_weread_shelf()

    assert created[0].started == 1 and created[0].shutdowns == 1


def test_js_error_raises_with_login_hint():
    page = FakeBskPage(
        payload={"error": "微信读书登录态失效（bookIds 返回 401），请在日常 Chrome 重新登录微信读书"}
    )
    session = FakeExternalSession(page)

    with pytest.raises(RuntimeError) as excinfo:
        shelf.fetch_weread_shelf(session=session)

    message = str(excinfo.value)
    assert "微信读书" in message
    assert "日常 Chrome" in message
    assert session.shutdowns == 0


def test_non_dict_payload_raises():
    page = FakeBskPage(payload=[])
    session = FakeExternalSession(page)

    with pytest.raises(RuntimeError) as excinfo:
        shelf.fetch_weread_shelf(session=session)

    assert "微信读书" in str(excinfo.value)


def test_debug_json_reflects_success():
    page = FakeBskPage(
        payload={"ids": 2, "books": [_book(), _book(bookId="b2", finishReading=1, readerLink=READER + "b2")], "badIds": ["mpbook"]}
    )
    shelf.fetch_weread_shelf(session=FakeExternalSession(page))

    debug = json.loads(shelf.DEBUG_JSON_PATH.read_text(encoding="utf-8"))
    assert debug["ok"] is True
    assert debug["count"] == 2
    assert debug["raw_count"] == 2
    assert debug["bad_ids"] == ["mpbook"]
    assert "error" not in debug


def test_debug_json_reflects_failure():
    page = FakeBskPage(payload={"error": "微信读书书架接口请求失败: boom"})
    with pytest.raises(RuntimeError):
        shelf.fetch_weread_shelf(session=FakeExternalSession(page))

    debug = json.loads(shelf.DEBUG_JSON_PATH.read_text(encoding="utf-8"))
    assert debug["ok"] is False
    assert debug["count"] == 0
    assert "boom" in debug["error"]


def test_deep_scan_with_bad_book_id_is_not_complete():
    """坏 bookId 无法确认对应书是否还在架：深扫不完整，宁可不做移除判定。"""
    page = FakeBskPage(payload={"ids": 1, "books": [_book()], "badIds": ["mpbook"]})
    batch = shelf.fetch_weread_shelf(session=FakeExternalSession(page), deep=True)

    assert batch.complete is False
    assert [item["url"] for item in batch.items] == [READER]


def test_incremental_weread_scan_is_never_complete():
    page = FakeBskPage(payload={"ids": 1, "books": [_book()], "badIds": []})
    batch = shelf.fetch_weread_shelf(session=FakeExternalSession(page))

    assert batch.complete is False


def test_incremental_skips_books_already_in_db():
    page = FakeBskPage(payload={"ids": 1, "books": [_book()], "badIds": []})
    batch = shelf.fetch_weread_shelf(
        session=FakeExternalSession(page), known_urls=[READER]
    )

    assert batch.items == []
    assert batch.complete is False

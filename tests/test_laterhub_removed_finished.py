"""laterhub「源内已移除」自动完成：DB 差集标记 + pipeline 安全闸门。

只有抓取成功且本次拿到完整列表（complete=True）才比对；抓取失败/半途而废一律不动。
全部打桩，不连真实网络/浏览器。
"""

from __future__ import annotations

import sqlite3

import pytest

from apps.laterhub import pipeline
from apps.laterhub.db import DBManager, LinkRecord
from apps.laterhub.sources import SOURCE_BY_ID
from connectors._shared.common import FetchBatch

BILI_SOURCE = "bilibili_watchlater"
XHH_SOURCE = "xiaoheihe_favor"
PRESENT = "https://www.bilibili.com/video/BV1present"
REMOVED = "https://www.bilibili.com/video/BV2removed"
OTHER_REMOVED = "https://www.bilibili.com/video/BV3other"
XHH_URL = "https://www.xiaoheihe.cn/app/bbs/link/999"


@pytest.fixture(autouse=True)
def _isolate_pipeline_io(monkeypatch):
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)
    monkeypatch.setattr(pipeline, "record_result", lambda *args, **kwargs: None)


def _add(db: DBManager, url: str, source: str, **marks: object) -> None:
    db.upsert_link(LinkRecord(url=url, title=f"标题 {url[-6:]}", source=source))
    if marks:
        assignments = ", ".join(f"{key} = ?" for key in marks)
        conn = sqlite3.connect(db.db_path)
        try:
            conn.execute(f"UPDATE links SET {assignments} WHERE url = ?", (*marks.values(), url))
            conn.commit()
        finally:
            conn.close()


def _state(db: DBManager, url: str) -> tuple[int, str | None, int]:
    conn = sqlite3.connect(db.db_path)
    try:
        row = conn.execute(
            "SELECT is_finished, finished_at, is_opened FROM links WHERE url = ?", (url,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0], row[1], row[2])


def _bili_fetcher(*urls: str, complete: bool = True):
    def fetcher(env_path, *, session=None, **kwargs):
        return FetchBatch(
            items=[{"url": url, "title": "视频", "source": BILI_SOURCE} for url in urls],
            complete=complete,
        )

    return fetcher


def test_marks_only_missing_unfinished_rows_of_that_source(tmp_path):
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _add(db, PRESENT, BILI_SOURCE)
    _add(db, REMOVED, BILI_SOURCE, is_opened=1)
    _add(db, OTHER_REMOVED, BILI_SOURCE, is_finished=1, finished_at="2026-10-01 08:00:00")
    _add(db, XHH_URL, XHH_SOURCE)

    finished = db.mark_missing_finished(BILI_SOURCE, {PRESENT})

    assert [row["url"] for row in finished] == [REMOVED]
    # 缺席的未完成行被标记完成，finished_at 落盘，is_opened 保留不动。
    removed_state = _state(db, REMOVED)
    assert removed_state[0] == 1
    assert removed_state[1]
    assert removed_state[2] == 1
    # 仍在列表里的行不受影响。
    assert _state(db, PRESENT)[0] == 0
    # 已完成的旧行 finished_at 不被覆盖。
    assert _state(db, OTHER_REMOVED) == (1, "2026-10-01 08:00:00", 0)
    # 其他源不受影响。
    assert _state(db, XHH_URL)[0] == 0


def test_pipeline_completes_missing_items_when_batch_complete(tmp_path):
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _add(db, PRESENT, BILI_SOURCE)
    _add(db, REMOVED, BILI_SOURCE)

    pipeline._fetch_source(
        source=SOURCE_BY_ID[BILI_SOURCE],
        fetcher=_bili_fetcher(PRESENT),
        db=db,
        deep=True,
    )

    assert _state(db, PRESENT)[0] == 0
    assert _state(db, REMOVED)[0] == 1


def test_pipeline_leaves_missing_items_alone_when_batch_incomplete(tmp_path, monkeypatch):
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _add(db, PRESENT, BILI_SOURCE)
    _add(db, REMOVED, BILI_SOURCE)
    logs: list[str] = []
    monkeypatch.setattr(pipeline, "log_line", logs.append)

    pipeline._fetch_source(
        source=SOURCE_BY_ID[BILI_SOURCE],
        fetcher=_bili_fetcher(PRESENT, complete=False),
        db=db,
        deep=True,
    )

    assert _state(db, REMOVED)[0] == 0
    # 跳过必须可见：否则翻页退化时功能会悄悄失效。
    assert any("未拿到完整列表" in line for line in logs)


def test_pipeline_skips_sources_without_finish_on_removed(tmp_path):
    db = DBManager(tmp_path / "laterhub.sqlite3")
    removed = "https://weread.qq.com/web/reader/abc"
    _add(db, removed, "weread_shelf")

    pipeline._fetch_source(
        source=SOURCE_BY_ID["weread_shelf"],
        fetcher=lambda env_path, *, session=None, **kwargs: FetchBatch(
            items=[{"url": "https://weread.qq.com/web/reader/def", "title": "新书", "source": "weread_shelf"}],
            complete=True,
        ),
        db=db,
    )

    assert _state(db, removed)[0] == 0


def test_refetch_revives_auto_finished_item(tmp_path):
    """自动完成不是终态：条目重新出现在源列表里，upsert 会把它改回未完成。"""
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _add(db, REMOVED, BILI_SOURCE)

    pipeline._fetch_source(
        source=SOURCE_BY_ID[BILI_SOURCE],
        fetcher=_bili_fetcher(PRESENT),
        db=db,
        deep=True,
    )
    assert _state(db, REMOVED)[0] == 1

    pipeline._fetch_source(
        source=SOURCE_BY_ID[BILI_SOURCE],
        fetcher=_bili_fetcher(PRESENT, REMOVED),
        db=db,
    )

    assert _state(db, REMOVED) == (0, None, 0)


def test_compare_after_skips_entries_recorded_before_cutoff(tmp_path):
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _add(db, PRESENT, BILI_SOURCE, created_at="2026-04-02T00:00:00+00:00")
    _add(db, REMOVED, BILI_SOURCE, created_at="2026-04-02T00:00:00+00:00")
    OLD = "https://www.bilibili.com/video/BV0old"
    _add(db, OLD, BILI_SOURCE, created_at="2026-03-24T05:22:16+00:00")

    finished = db.mark_missing_finished(
        BILI_SOURCE, {PRESENT}, compare_after="2026-03-24T08:48:36+00:00"
    )

    assert [row["url"] for row in finished] == [REMOVED]
    # 早于下界的条目保持未完成，不参与对照。
    assert _state(db, OLD)[0] == 0


def test_incremental_batch_never_removes_even_when_complete(tmp_path):
    """日常短抓取即使抓取器自报 complete=True，也不得进入移除判定。"""
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _add(db, PRESENT, BILI_SOURCE)
    _add(db, REMOVED, BILI_SOURCE)

    pipeline._fetch_source(
        source=SOURCE_BY_ID[BILI_SOURCE],
        fetcher=_bili_fetcher(PRESENT, complete=True),
        db=db,
    )

    assert _state(db, REMOVED)[0] == 0

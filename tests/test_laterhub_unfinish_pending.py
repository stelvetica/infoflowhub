"""面板「未完成」按钮：取消完成 = 回到「未处理」（连已打开一起清）。

「未处理」视图 = 未完成 且 未打开（见 views._filter_laterhub_rows），只清完成态会让
该行掉出「未处理」「已完成」两个视图，等于没生效。
"""

from __future__ import annotations

import sqlite3

import web.services.views as views
from apps.laterhub.db import DBManager, LinkRecord

URL = "https://www.xiaoheihe.cn/app/bbs/link/123"
URL2 = "https://www.bilibili.com/video/BV1xx411c7mD"
FINISHED_AT = "2026-10-08 15:45:51"
OPENED_AT = "2026-10-08 15:33:31"


def _seed(db_path, url: str = URL, *, source: str = "xiaoheihe_favor") -> int:
    db = DBManager(db_path)
    return db.upsert_link(LinkRecord(url=url, title="帖子", source=source))


def _mark(db_path, link_id: int, **values: object) -> None:
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"UPDATE links SET {assignments} WHERE id = ?", (*values.values(), link_id))
        conn.commit()
    finally:
        conn.close()


def _state(db_path, link_id: int) -> tuple[int, str | None, int, str | None]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT is_finished, finished_at, is_opened, opened_at FROM links WHERE id = ?", (link_id,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0], row[1], row[2], row[3])


def _unfinished_view_ids() -> set[int]:
    rows, _, _, _, _ = views._filter_laterhub_rows({"laterhub_filter_finished": "0"})
    return {int(row["id"]) for row in rows}


def test_unfinish_sends_item_back_to_pending(tmp_path, monkeypatch):
    db_path = tmp_path / "laterhub.sqlite3"
    link_id = _seed(db_path)
    _mark(db_path, link_id, is_finished=1, finished_at=FINISHED_AT, is_opened=1, opened_at=OPENED_AT)
    monkeypatch.setattr(views, "LATERHUB_DB_PATH", db_path)
    assert link_id not in _unfinished_view_ids()

    views.mark_laterhub_finished(link_id, False)

    assert _state(db_path, link_id) == (0, None, 0, None)
    assert link_id in _unfinished_view_ids()


def test_bulk_unfinish_sends_items_back_to_pending(tmp_path, monkeypatch):
    db_path = tmp_path / "laterhub.sqlite3"
    ids = [_seed(db_path, URL), _seed(db_path, URL2, source="bilibili_watchlater")]
    for link_id in ids:
        _mark(db_path, link_id, is_finished=1, finished_at=FINISHED_AT, is_opened=1, opened_at=OPENED_AT)
    monkeypatch.setattr(views, "LATERHUB_DB_PATH", db_path)

    changed = views.mark_laterhub_finished_bulk(ids, False)

    assert changed == 2
    assert [_state(db_path, link_id) for link_id in ids] == [(0, None, 0, None)] * 2


def test_finish_keeps_opened_flag(tmp_path, monkeypatch):
    db_path = tmp_path / "laterhub.sqlite3"
    link_id = _seed(db_path)
    _mark(db_path, link_id, is_opened=1, opened_at=OPENED_AT)
    monkeypatch.setattr(views, "LATERHUB_DB_PATH", db_path)

    views.mark_laterhub_finished(link_id, True)

    is_finished, finished_at, is_opened, opened_at = _state(db_path, link_id)
    assert (is_finished, is_opened, opened_at) == (1, 1, OPENED_AT)
    assert finished_at

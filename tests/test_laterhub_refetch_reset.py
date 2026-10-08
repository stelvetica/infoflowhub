"""laterhub 抓取再次命中同一条目时，本地状态如何回落。

规则：源列表里还抓得到 = 这条在本项目里不算处理完，已完成必须改回未完成；
且清完成时连「已打开」一起清，否则它会卡在「未处理」「已完成」两个视图之外。
"""

from __future__ import annotations

import sqlite3

from apps.laterhub.db import DBManager, LinkRecord

BILI = "https://www.bilibili.com/video/BV1xx411c7mD"
FINISHED_AT = "2026-10-06 23:12:04"
OPENED_AT = "2026-10-01 08:00:00"


def _refetch(db: DBManager, url: str, *, source: str = "bilibili_watchlater") -> None:
    db.upsert_link(LinkRecord(url=url, title="视频", source=source))


def _mark(db: DBManager, url: str, **values: object) -> None:
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = sqlite3.connect(db.db_path)
    try:
        conn.execute(f"UPDATE links SET {assignments} WHERE url = ?", (*values.values(), url))
        conn.commit()
    finally:
        conn.close()


def _state(db: DBManager, url: str) -> tuple[int, str | None, int, str | None]:
    conn = sqlite3.connect(db.db_path)
    try:
        row = conn.execute(
            "SELECT is_finished, finished_at, is_opened, opened_at FROM links WHERE url = ?", (url,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0], row[1], row[2], row[3])


def test_refetch_resets_finished_item(tmp_path):
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _refetch(db, BILI)
    _mark(db, BILI, is_finished=1, finished_at=FINISHED_AT)

    _refetch(db, BILI)

    assert _state(db, BILI) == (0, None, 0, None)


def test_refetch_clears_opened_flag_of_finished_item(tmp_path):
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _refetch(db, BILI)
    _mark(db, BILI, is_finished=1, finished_at=FINISHED_AT, is_opened=1, opened_at=OPENED_AT)

    _refetch(db, BILI)

    assert _state(db, BILI) == (0, None, 0, None)


def test_refetch_keeps_opened_flag_of_unfinished_item(tmp_path):
    db = DBManager(tmp_path / "laterhub.sqlite3")
    _refetch(db, BILI)
    _mark(db, BILI, is_opened=1, opened_at=OPENED_AT)

    _refetch(db, BILI)

    assert _state(db, BILI) == (0, None, 1, OPENED_AT)

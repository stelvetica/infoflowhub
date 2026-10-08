"""稍后读「状态」筛选：下拉只有 未完成/全部，默认未完成。

未完成 = 所有没标完成的（点开过也算，读态只决定链接样式）——与行内按钮的
「完成/未完成」同一口径；否则一条按钮写着「未完成」的条目会不在「未完成」筛选里。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import web.services.views as views
from apps.laterhub.db import DBManager, LinkRecord

BASE_DIR = Path(__file__).resolve().parents[1]
PANEL_TEMPLATE = BASE_DIR / "web" / "templates" / "partials" / "laterhub_panel.html"


def _mark(db_path, link_id: int, **values: object) -> None:
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"UPDATE links SET {assignments} WHERE id = ?", (*values.values(), link_id))
        conn.commit()
    finally:
        conn.close()


def _seed(db_path) -> dict[str, int]:
    db = DBManager(db_path)
    ids = {
        "plain": db.upsert_link(LinkRecord(url="https://www.bilibili.com/video/BV1", title="没点开", source="bilibili_watchlater")),
        "opened": db.upsert_link(LinkRecord(url="https://www.bilibili.com/video/BV2", title="点开过", source="bilibili_watchlater")),
        "finished": db.upsert_link(LinkRecord(url="https://www.bilibili.com/video/BV3", title="已完成", source="bilibili_watchlater")),
    }
    _mark(db_path, ids["opened"], is_opened=1, opened_at="2026-10-01 08:00:00")
    _mark(db_path, ids["finished"], is_finished=1, finished_at="2026-10-02 08:00:00")
    return ids


def _filtered_ids(filter_value: str | None) -> set[int]:
    query = {} if filter_value is None else {"laterhub_filter_finished": filter_value}
    rows, _, _, _, _ = views._filter_laterhub_rows(query)
    return {int(row["id"]) for row in rows}


def test_unfinished_is_default_and_includes_opened_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "laterhub.sqlite3"
    ids = _seed(db_path)
    monkeypatch.setattr(views, "LATERHUB_DB_PATH", db_path)

    assert views.build_laterhub_state_params({})["laterhub_filter_finished"] == "0"
    assert _filtered_ids(None) == {ids["plain"], ids["opened"]}
    assert _filtered_ids("0") == {ids["plain"], ids["opened"]}
    assert _filtered_ids("") == set(ids.values())


def test_status_select_only_exposes_unfinished_and_all():
    select = PANEL_TEMPLATE.read_text(encoding="utf-8").split('name="laterhub_filter_finished"', 1)[1].split("</select>", 1)[0]
    options = [line.strip() for line in select.splitlines() if "<option" in line]

    assert len(options) == 2
    assert 'value="0"' in options[0] and ">未完成</option>" in options[0]
    assert 'value=""' in options[1] and ">全部</option>" in options[1]
    assert "未处理" not in select

"""手机端快速收录：链接走原路径，纯文字用 note:// 内容哈希占位并同池入库。

覆盖三点：note 条目可检索的 url 形状、同文去重、面板渲染标记 is_note。
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

import web.routes.main as main_routes
import web.services.views as views


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "laterhub.sqlite3"
    monkeypatch.setattr(main_routes, "DB_PATH", path)
    monkeypatch.setattr(views, "LATERHUB_DB_PATH", path)
    return path


def test_note_uses_stable_hash_url(db_path):
    result = main_routes.add_laterhub_note("一段想存下来的话")

    assert result["kind"] == "note"
    assert result["duplicated"] is False
    with sqlite3.connect(db_path) as conn:
        url, title, source = conn.execute("SELECT url, title, source FROM links").fetchone()
    assert url == f"note://{hashlib.sha1('一段想存下来的话'.encode()).hexdigest()[:16]}"
    assert title == "一段想存下来的话"
    assert source == "manual_verify"


def test_same_note_is_deduplicated(db_path):
    first = main_routes.add_laterhub_note("同一段话")
    second = main_routes.add_laterhub_note("  同一段话\n")

    assert first["id"] == second["id"]
    assert second["duplicated"] is True


def test_blank_and_oversized_notes_rejected(db_path):
    with pytest.raises(ValueError):
        main_routes.add_laterhub_note("   ")
    with pytest.raises(ValueError):
        main_routes.add_laterhub_note("字" * (main_routes.NOTE_MAX_CHARS + 1))


def test_panel_rows_flag_notes_but_not_links(db_path):
    main_routes.add_laterhub_note("面板里只显示一行摘要")
    main_routes.add_laterhub_link("https://example.com/article", "普通链接")

    rows, _, _ = views._build_laterhub_rows()
    by_title = {row["title"]: row for row in rows}

    assert by_title["面板里只显示一行摘要"]["is_note"] is True
    assert by_title["普通链接"]["is_note"] is False


def test_quick_page_and_panel_fragment(db_path):
    from fastapi.testclient import TestClient
    from web.app import app

    client = TestClient(app)  # 不进 with：不触发 lifespan，避免拉起定时任务

    page = client.get("/quick")
    assert page.status_code == 200
    assert "发到稍后读" in page.text

    response = client.post("/actions/laterhub/add-link", json={"text": "手机发来的一段话"})
    assert response.status_code == 200
    assert response.json()["kind"] == "note"

    fragment = client.get("/fragments/laterhub")
    assert fragment.status_code == 200
    assert 'class="cell-kind-badge"' in fragment.text
    # 纯文字不得变成可点链接（note:// 是内部占位，不出现在页面上）
    assert "note://" not in fragment.text

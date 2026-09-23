"""资讯类来源（来源名以「资讯」开头）的条目默认已读：不进未读列表，且渲染为已读样式。"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from web.services import views

TEMPLATES_DIR = Path(views.__file__).resolve().parents[1] / "templates"
INFO_TITLE = "全球蓝宝书 9月23日全球版"
OPINION_TITLE = "来点大A笑话"

SOURCES = [
    {"id": "alphapai", "name": "资讯 Alpha派蓝宝书", "enabled": True, "site_url": ""},
    {"id": "mark-huang", "name": "观点 成长股 Mark_Huang", "enabled": True, "site_url": ""},
]


def _entry(source_id: str, title: str, published_at: str) -> dict:
    return {
        "source_id": source_id,
        "source_name": "",
        "title": title,
        "link": f"https://example.com/{source_id}",
        "published_at": published_at,
        "published": "",
        "summary": "",
        "markdown_path": "",
        "created_at": published_at,
    }


ENTRIES = [
    _entry("alphapai", INFO_TITLE, "2026-09-23 08:00:00"),
    _entry("mark-huang", OPINION_TITLE, "2026-09-23 09:00:00"),
]


def _stub_sources_and_entries(monkeypatch) -> None:
    monkeypatch.setattr(views, "normalize_sources", lambda include_auth=True: SOURCES)
    monkeypatch.setattr(views, "_load_entries", lambda limit=500: ENTRIES)
    monkeypatch.setattr(views, "_count_entries", lambda: len(ENTRIES))
    monkeypatch.setattr(views, "list_read_entry_keys", lambda: set())


def _anchor_html(html: str, title: str) -> str:
    matched = re.search(rf'<a [^>]*title="{re.escape(title)}"[^>]*>', html)
    assert matched, f"未渲染出 {title} 的标题链接"
    return matched.group(0)


def test_info_entries_are_not_listed_as_unread(monkeypatch):
    _stub_sources_and_entries(monkeypatch)
    view = views.get_entries_view({"entries_unread_only": "1"})
    assert [row["title"] for row in view["rows"]] == [OPINION_TITLE]


def test_info_entries_stay_listed_but_flagged_read(monkeypatch):
    _stub_sources_and_entries(monkeypatch)
    view = views.get_entries_view({"entries_unread_only": ""})
    assert {row["title"]: row["default_read"] for row in view["rows"]} == {INFO_TITLE: True, OPINION_TITLE: False}


def test_info_entries_render_read_without_read_track(monkeypatch):
    """渲染契约：资讯条目给 cell-read 且不挂 read-track（挂了会被 app.js 重判成未读）。"""
    _stub_sources_and_entries(monkeypatch)
    view = views.get_entries_view({"entries_unread_only": ""})
    html = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR))).get_template("partials/entries_table.html").render(entries=view, params={})
    info_anchor = _anchor_html(html, INFO_TITLE)
    assert "cell-read" in info_anchor and "read-track" not in info_anchor
    assert "cell-unread read-track" in _anchor_html(html, OPINION_TITLE)
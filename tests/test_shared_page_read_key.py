"""共享页面源（一个聚合页挂多条条目）的已读口径：read_key 必须按标题区分，
一条已读不得连坐整源——否则未读视图里整源消失（点过任意一条即全源不见）。
"""

from __future__ import annotations

from web.services import views
from web.services.utils import read_entry_key, read_link_key

SOCIAL_URL = "https://alphapai-web.rabyte.cn/reading/social-media/index"
TITLE_A = "水木纪要｜花旗：光通信下一个机会-OCS"
TITLE_B = "水木纪要｜大摩闭门会：银行看修复，地产等回款"

SOURCES = [{"id": "alphapai-social-media", "name": "公众号 Alpha派公众号", "enabled": True, "site_url": SOCIAL_URL}]


def _entry(title: str) -> dict:
    return {
        "source_id": "alphapai-social-media",
        "source_name": "",
        "title": title,
        "link": SOCIAL_URL,
        "published_at": "2026-10-07 14:00:00",
        "published": "",
        "summary": "",
        "markdown_path": "",
        "created_at": "2026-10-07 14:00:00",
    }


def test_same_page_entries_get_distinct_read_keys():
    key_a = read_entry_key("alphapai-social-media", SOCIAL_URL, TITLE_A)
    key_b = read_entry_key("alphapai-social-media", SOCIAL_URL, TITLE_B)
    assert key_a and key_b and key_a != key_b
    # 旧算法（整源共用 sha1(link)）会把两条撞成一把钥匙
    assert key_a != read_link_key(SOCIAL_URL)


def test_one_read_entry_does_not_hide_siblings(monkeypatch):
    entries = [_entry(TITLE_A), _entry(TITLE_B)]
    monkeypatch.setattr(views, "normalize_sources", lambda include_auth=True: SOURCES)
    monkeypatch.setattr(views, "_load_entries", lambda limit=500: entries)
    monkeypatch.setattr(views, "_count_entries", lambda: len(entries))
    monkeypatch.setattr(
        views,
        "list_read_entry_keys",
        lambda: {read_entry_key("alphapai-social-media", SOCIAL_URL, TITLE_A)},
    )
    view = views.get_entries_view({"entries_unread_only": "1"})
    assert [row["title"] for row in view["rows"]] == [TITLE_B]

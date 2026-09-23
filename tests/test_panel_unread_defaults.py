"""两个面板的未读筛选默认刻意不同:订阅缺省只看未读,新闻缺省显示全部。"""

from __future__ import annotations

from web.services.views import build_entries_state_params, build_news_state_params


def test_entries_defaults_to_unread_only():
    assert build_entries_state_params({})["entries_unread_only"] == "1"


def test_news_defaults_to_show_all():
    assert build_news_state_params({})["news_unread_only"] == "0"


def test_explicit_choice_is_respected_on_both_panels():
    assert build_entries_state_params({"entries_unread_only": ""})["entries_unread_only"] == ""
    assert build_news_state_params({"news_unread_only": "1"})["news_unread_only"] == "1"
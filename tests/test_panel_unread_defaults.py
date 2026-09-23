"""两个面板的未读筛选口径:订阅缺省只看未读且可切换,新闻恒为显示全部(切换控件已移除)。"""

from __future__ import annotations

from web.services.views import build_entries_state_params, build_news_state_params


def test_entries_defaults_to_unread_only():
    assert build_entries_state_params({})["entries_unread_only"] == "1"


def test_news_always_shows_all():
    assert build_news_state_params({})["news_unread_only"] == "0"


def test_entries_explicit_choice_is_respected():
    assert build_entries_state_params({"entries_unread_only": ""})["entries_unread_only"] == ""
    assert build_entries_state_params({"entries_unread_only": "1"})["entries_unread_only"] == "1"


def test_news_ignores_stale_unread_cookie():
    """新闻面板的未读控件已移除:浏览器里残留的旧 cookie 不能把产物静默过滤掉。"""
    assert build_news_state_params({"news_unread_only": "1"})["news_unread_only"] == "0"
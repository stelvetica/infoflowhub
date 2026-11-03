from __future__ import annotations

from web.services import views


def test_keyword_search_uses_full_history_window(monkeypatch) -> None:
    limits: list[int] = []
    monkeypatch.setattr(
        views, "_iter_entry_rows", lambda **kwargs: limits.append(kwargs["load_limit"]) or []
    )
    monkeypatch.setattr(views, "_count_entries", lambda: 0)
    monkeypatch.setattr(views, "list_read_entry_keys", lambda: set())

    views.get_entries_view({"entries_q": "十三邀"})
    views.get_entries_view({})

    assert limits == [views.ENTRIES_SEARCH_LOAD_LIMIT, views.ENTRIES_LOAD_LIMIT]
    # 搜索窗口必须覆盖全量历史，否则超过 500 条的旧条目无法命中关键字。
    assert views.ENTRIES_SEARCH_LOAD_LIMIT > views.ENTRIES_LOAD_LIMIT

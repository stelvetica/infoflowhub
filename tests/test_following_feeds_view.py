from __future__ import annotations

from web.services import views


AGGREGATE_SITE_URL = "https://t.bilibili.com/"
NORMAL_SITE_URL = "https://space.bilibili.com/12345"


def _item(source_id: str, source_name: str) -> dict:
    return {
        "source_id": source_id,
        "source_name": source_name,
        "title": "标题",
        "link": "https://example.com/1",
        "published_at": "2026-06-01 10:00:00",
        "summary": "摘要",
        "markdown_path": "",
    }


def test_aggregate_source_prefers_entry_author_name() -> None:
    row = views._build_entry_row(
        _item("agg", "某UP"),
        {"agg": "B站关注动态"},
        {"agg": AGGREGATE_SITE_URL},
        {"agg"},
    )
    assert row["source_name"] == "某UP"


def test_aggregate_source_falls_back_to_config_name_when_entry_name_empty() -> None:
    row = views._build_entry_row(
        _item("agg", ""),
        {"agg": "B站关注动态"},
        {"agg": AGGREGATE_SITE_URL},
        {"agg"},
    )
    assert row["source_name"] == "B站关注动态"


def test_normal_source_keeps_config_name_priority() -> None:
    row = views._build_entry_row(
        _item("normal", "旧名字"),
        {"normal": "新名字"},
        {"normal": NORMAL_SITE_URL},
        {"agg"},
    )
    assert row["source_name"] == "新名字"


def test_build_entry_row_three_arg_call_still_works() -> None:
    row = views._build_entry_row(
        _item("normal", "旧名字"),
        {"normal": "新名字"},
        {"normal": NORMAL_SITE_URL},
    )
    assert row["source_name"] == "新名字"


def _source(source_id: str, name: str, site_url: str) -> dict:
    return {
        "id": source_id,
        "name": name,
        "group": "",
        "feed_url": "",
        "site_url": site_url,
        "provider": "web",
        "fetch_via": "web",
        "kind": "dynamic",
        "enabled": True,
        "note": "",
        "channel": "bilibili",
        "auth_key": "",
        "fallback_mode": "none",
    }


def test_save_source_rename_skipped_for_aggregate(monkeypatch) -> None:
    existing = _source("agg", "旧配置名", AGGREGATE_SITE_URL)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(views, "load_sources", lambda: [])
    monkeypatch.setattr(views, "load_source_catalog", lambda: [existing])
    monkeypatch.setattr(views, "save_sources", lambda items: None)
    monkeypatch.setattr(views, "rename_source", lambda sid, name: calls.append((sid, name)) or 0)

    views.save_source(
        {
            "source_id": "agg",
            "name": "B站关注动态",
            "feed_url": AGGREGATE_SITE_URL,
            "site_url": AGGREGATE_SITE_URL,
        }
    )
    assert calls == []


def test_save_source_rename_still_runs_for_normal_source(monkeypatch) -> None:
    existing = _source("normal", "旧配置名", NORMAL_SITE_URL)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(views, "load_sources", lambda: [])
    monkeypatch.setattr(views, "load_source_catalog", lambda: [existing])
    monkeypatch.setattr(views, "save_sources", lambda items: None)
    monkeypatch.setattr(views, "rename_source", lambda sid, name: calls.append((sid, name)) or 0)

    views.save_source(
        {
            "source_id": "normal",
            "name": "新配置名",
            "feed_url": NORMAL_SITE_URL,
            "site_url": NORMAL_SITE_URL,
        }
    )
    assert calls == [("normal", "新配置名")]


def test_iter_entry_rows_shows_enabled_aggregate_hides_disabled(monkeypatch) -> None:
    sources = [
        {**_source("agg", "B站关注动态", AGGREGATE_SITE_URL), "enabled": True},
        {**_source("old", "旧UP", "https://space.bilibili.com/999"), "enabled": False},
    ]
    entries = [_item("agg", "某UP"), _item("old", "旧UP")]
    monkeypatch.setattr(views, "normalize_sources", lambda **kwargs: sources)
    monkeypatch.setattr(views, "_load_entries", lambda limit: entries)

    rows = views._iter_entry_rows(
        source_ids_filter=None,
        keyword="",
        unread_only=False,
        effective_read_keys=set(),
        load_limit=10,
    )
    assert [row["source_id"] for row in rows] == ["agg"]
    assert rows[0]["source_name"] == "某UP"

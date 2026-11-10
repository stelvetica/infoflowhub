"""laterhub 源管理：启用配置、健康记录、pipeline 跳过停用源、设置页数据。

全部隔离到 tmp_path，不写真实 config/runtime。
"""

from __future__ import annotations

import argparse
import json

import pytest

from apps.laterhub import health, pipeline, sources
from apps.laterhub.sources import SOURCES
from web.services import views


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "SOURCES_PATH", tmp_path / "config" / "laterhub_sources.json")
    monkeypatch.setattr(health, "HEALTH_PATH", tmp_path / "runtime" / "health" / "laterhub_source_health.json")


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        retry_failed=False,
        fetch_bilibili=False,
        fetch_douyin=False,
        fetch_xiaoheihe=False,
        fetch_weread=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class FakeDB:
    def __init__(self) -> None:
        self.saved: list = []

    def upsert_link(self, record) -> None:
        self.saved.append(record)


def test_missing_config_enables_every_source() -> None:
    assert sources.load_enabled_map() == {source.id: True for source in SOURCES}


def test_malformed_entry_falls_back_to_enabled() -> None:
    sources.SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    sources.SOURCES_PATH.write_text(
        json.dumps({"sources": [{"id": "weread_shelf", "enabled": "yes"}, {"id": "unknown", "enabled": False}]}),
        encoding="utf-8",
    )
    assert sources.load_enabled_map() == {source.id: True for source in SOURCES}


def test_set_enabled_persists_and_unknown_id_raises() -> None:
    sources.set_enabled("weread_shelf", False)

    assert sources.load_enabled_map()["weread_shelf"] is False
    assert sources.is_enabled("weread_shelf") is False
    payload = json.loads(sources.SOURCES_PATH.read_text(encoding="utf-8"))
    assert [row["id"] for row in payload["sources"]] == [source.id for source in SOURCES]
    assert payload["sources"][3] == {"id": "weread_shelf", "enabled": False}

    with pytest.raises(KeyError):
        sources.set_enabled("not_registered", True)


def test_record_result_success_and_failure() -> None:
    health.record_result("bilibili_watchlater", ok=False, error="  boom\n\n  detail  ")
    failed_row = health.load_health()["bilibili_watchlater"]
    assert failed_row["last_failed_at"]
    assert failed_row["last_checked_at"]
    assert failed_row["last_error"] == "boom detail"

    health.record_result("bilibili_watchlater", ok=True)
    row = health.load_health()["bilibili_watchlater"]
    assert row["last_success_at"]
    assert row["last_error"] == ""
    # 成功不清除历史失败时间，供设置页计算失效天数。
    assert row["last_failed_at"] == failed_row["last_failed_at"]


def test_disabled_source_is_skipped(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)
    monkeypatch.setattr(pipeline, "fetch_bilibili_watchlater", lambda *args, **kwargs: calls.append(1) or [])
    sources.set_enabled("bilibili_watchlater", False)

    scheduled = pipeline._fetch_enabled_sources(args=_args(fetch_bilibili=True), db=FakeDB(), session=None)

    assert calls == []
    assert scheduled is False


def test_enabled_source_success_records_health(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)
    monkeypatch.setattr(
        pipeline,
        "fetch_bilibili_watchlater",
        lambda env_path, *, session=None, **kwargs: [{"url": "u1", "title": "t1", "source": "bilibili_watchlater"}],
    )

    scheduled = pipeline._fetch_enabled_sources(args=_args(fetch_bilibili=True), db=FakeDB(), session=None)

    assert scheduled is True
    assert health.load_health()["bilibili_watchlater"]["last_success_at"]


def test_bsk_start_failure_records_health(monkeypatch) -> None:
    """bsk 会话起不来也要落到「最近错误」，否则管理表显示一切正常。"""
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)

    class BrokenBsk:
        def __init__(self, *, start_url="about:blank") -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("daemon 未就绪")

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr(pipeline, "BskRunnerSession", BrokenBsk)

    scheduled = pipeline._fetch_enabled_sources(args=_args(fetch_douyin=True), db=FakeDB(), session=None)

    assert scheduled is True
    assert "bsk 会话启动失败" in health.load_health()["douyin_favorite"]["last_error"]


def test_settings_view_lists_four_registered_sources(monkeypatch) -> None:
    monkeypatch.setattr(views, "list_auth_statuses", lambda: [])
    monkeypatch.setattr(views, "migrate_legacy_source_ids", lambda: 0)

    rows = views.get_settings_view({})["laterhub_management_sources"]

    assert [row["id"] for row in rows] == [source.id for source in SOURCES]

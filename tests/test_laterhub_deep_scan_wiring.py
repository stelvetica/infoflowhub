"""深度扫描接线：注册表标记、deep 参数透传、完成即记账、超时预算与手动路径固定增量。"""

from __future__ import annotations

import argparse
import types

import pytest

from apps.laterhub import pipeline
from apps.laterhub.sources import SOURCE_BY_ID, SOURCES
from connectors._shared.common import FetchBatch
from web.services import fetch_runtime


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(pipeline, "log_line", lambda message: None)
    monkeypatch.setattr(pipeline, "record_result", lambda *args, **kwargs: None)


class FakeDB:
    def __init__(self) -> None:
        self.saved: list = []
        self.mark_calls: list[dict] = []

    def upsert_link(self, record) -> None:
        self.saved.append(record)

    def mark_missing_finished(self, source_id, urls, **kwargs):
        self.mark_calls.append({"source": source_id, "kwargs": kwargs})
        return []


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


def test_registry_marks_only_douyin_as_deep_capable():
    douyin = SOURCE_BY_ID["douyin_favorite"]
    assert douyin.finish_on_removed is True
    assert douyin.deep_scan_interval_days == 30
    assert douyin.compare_after == "2026-03-24T08:48:36+00:00"
    for source in SOURCES:
        if source.id == "douyin_favorite":
            continue
        assert source.deep_scan_interval_days is None, source.id
        assert source.compare_after is None, source.id


def test_douyin_removal_comparison_passes_compare_after(monkeypatch):
    monkeypatch.setattr(pipeline.scan_state, "record_deep_scan_done", lambda *a, **k: None)
    db = FakeDB()

    pipeline._fetch_source(
        source=SOURCE_BY_ID["douyin_favorite"],
        fetcher=lambda env_path, *, session=None, deep=False: FetchBatch(
            items=[], complete=True
        ),
        db=db,
        deep=True,
        supports_deep=True,
    )

    assert db.mark_calls == [
        {"source": "douyin_favorite", "kwargs": {"compare_after": "2026-03-24T08:48:36+00:00"}}
    ]


def test_fetch_source_passes_deep_only_when_asked(monkeypatch):
    seen: list[bool] = []

    def deep_fetcher(env_path, *, session=None, deep=False):
        seen.append(deep)
        return FetchBatch(items=[], complete=False)

    monkeypatch.setattr(pipeline.scan_state, "record_deep_scan_done", lambda *a, **k: None)

    pipeline._fetch_source(
        source=SOURCE_BY_ID["douyin_favorite"],
        fetcher=deep_fetcher,
        db=FakeDB(),
        deep=True,
        supports_deep=True,
    )
    assert seen == [True]


def test_fetch_source_without_deep_does_not_pass_deep_keyword():
    seen: list[str] = []

    def legacy_fetcher(env_path, *, session=None):
        seen.append("called")
        return FetchBatch(items=[], complete=False)

    pipeline._fetch_source(
        source=SOURCE_BY_ID["douyin_favorite"],
        fetcher=legacy_fetcher,
        db=FakeDB(),
    )
    assert seen == ["called"]


@pytest.mark.parametrize("complete, expected_calls", [(True, 1), (False, 0)])
def test_deep_scan_records_done_only_when_complete(monkeypatch, complete, expected_calls):
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline.scan_state, "record_deep_scan_done", lambda source_id: calls.append(source_id)
    )

    pipeline._fetch_source(
        source=SOURCE_BY_ID["douyin_favorite"],
        fetcher=lambda env_path, *, session=None, deep=False: FetchBatch(
            items=[], complete=complete
        ),
        db=FakeDB(),
        deep=True,
        supports_deep=True,
    )

    assert calls == ["douyin_favorite"] * expected_calls


@pytest.mark.parametrize(
    "deep, expected",
    [(True, pipeline.SOURCE_DEEP_SCAN_TIMEOUT_SECONDS), (False, pipeline.SOURCE_FETCH_TIMEOUT_SECONDS)],
)
def test_fetch_source_timeout_budget_depends_on_deep(monkeypatch, deep, expected):
    captured: dict = {}

    def fake_runner(label, fetcher, session=None, **kwargs):
        captured.update(kwargs)
        return FetchBatch(items=[], complete=False)

    monkeypatch.setattr(pipeline, "_run_fetcher_with_timeout", fake_runner)

    pipeline._fetch_source(
        source=SOURCE_BY_ID["douyin_favorite"],
        fetcher=lambda env_path, *, session=None, deep=False: FetchBatch(items=[], complete=False),
        db=FakeDB(),
        deep=deep,
        supports_deep=True,
    )

    assert captured["timeout_seconds"] == expected
    assert captured["deep"] is deep


def test_automatic_flow_uses_due_check_and_manual_forces_incremental(monkeypatch):
    calls: list[bool] = []

    class FakeBsk:
        def __init__(self, *, start_url="about:blank") -> None:
            self.browser = types.SimpleNamespace(session_id="s1")

        def start(self) -> None:
            pass

        def shutdown(self) -> None:
            pass

    def douyin_fetcher(env_path, *, session=None, deep=False):
        calls.append(deep)
        return FetchBatch(items=[], complete=False)

    monkeypatch.setattr(pipeline, "BskRunnerSession", FakeBsk)
    monkeypatch.setattr(pipeline, "fetch_douyin_favorites", douyin_fetcher)
    monkeypatch.setattr(pipeline.scan_state, "deep_scan_due", lambda *a, **k: True)

    pipeline._fetch_enabled_sources(args=_args(fetch_douyin=True), db=FakeDB(), session=None)
    pipeline._fetch_enabled_sources(
        args=_args(fetch_douyin=True), db=FakeDB(), session=None, deep_scan=False
    )

    assert calls == [True, False]


def test_fetch_laterhub_now_propagates_manual_incremental(monkeypatch):
    captured: dict = {}

    def fake_run_main_flow(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(fetched_sources=4, pending_total=0, push_enabled=False)

    monkeypatch.setattr(fetch_runtime, "run_main_flow", fake_run_main_flow)

    fetch_runtime.fetch_laterhub_now(deep_scan=False)

    assert captured["deep_scan"] is False
    assert captured["fetch_douyin"] is True

"""8:10 晨间流程的抓取范围：只补「资讯」前缀源，不碰全量订阅与稍后读。"""

from __future__ import annotations

import pytest

from apps.subscriptions import rss_config
from web.services import auto_runner, fetch_runtime


SOURCES = [
    {"id": "alphapai", "name": "资讯 Alpha派蓝宝书", "enabled": True},
    {"id": "jiuyangongshe_jiucaituanzi", "name": "资讯 韭菜团子", "enabled": True},
    {"id": "alphapai-social-media", "name": "公众号 Alpha派公众号", "enabled": True},
    {"id": "opinion", "name": "观点 某博主", "enabled": True},
    {"id": "disabled-news", "name": "资讯 已停用", "enabled": False},
]


class _Outcome:
    def __init__(self, success_sources: int, failures: list[str], inserted: int = 0) -> None:
        self.success_sources = success_sources
        self.failures = failures
        self.warnings: list[str] = []
        self.inserted_total = inserted


def test_fetch_news_now_selects_only_news_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_run(selected, **kwargs):
        seen["ids"] = [item["id"] for item in selected]
        seen["persist_global"] = kwargs.get("persist_global")
        return _Outcome(success_sources=2, failures=[], inserted=7)

    monkeypatch.setattr(fetch_runtime, "load_sources", lambda: SOURCES)
    monkeypatch.setattr(fetch_runtime, "run_source_fetch", fake_run)

    result = fetch_runtime.fetch_news_now()

    assert seen["ids"] == ["alphapai", "jiuyangongshe_jiucaituanzi"]
    # 全站「成功源数」由 7:10 的全量抓取负责，8:10 的 2 源不能覆写它
    assert seen["persist_global"] is False
    assert result == {
        "fetch_state": "success",
        "last_total_sources": 2,
        "last_success_sources": 2,
        "last_inserted_entries": 7,
        "last_error": "",
    }


def test_fetch_news_now_reports_missing_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_runtime, "load_sources", lambda: SOURCES[2:])
    monkeypatch.setattr(
        fetch_runtime,
        "run_source_fetch",
        lambda *a, **k: pytest.fail("没有资讯源时不应发起抓取"),
    )

    result = fetch_runtime.fetch_news_now()

    assert result["fetch_state"] == "error"
    assert rss_config.NEWS_SOURCE_PREFIX in result["last_error"]


def test_morning_fetch_helper_skips_full_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """8:10 的取数入口只走资讯源，不碰全量订阅与稍后读。"""
    monkeypatch.setattr(auto_runner, "fetch_news_now", lambda: {"success_sources": 1})
    monkeypatch.setattr(auto_runner, "fetch_now", lambda: pytest.fail("8:10 不应全量抓订阅"))
    monkeypatch.setattr(auto_runner, "fetch_laterhub_now", lambda: pytest.fail("8:10 不应抓稍后读"))

    assert auto_runner._fetch_news() == {"subscriptions": {"success_sources": 1}}

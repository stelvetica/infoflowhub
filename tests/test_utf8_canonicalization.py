from __future__ import annotations

from apps.subscriptions import rss_db, runtime_health
from apps.subscriptions.models import FeedEntry, FeedFetchResult
from web.services import views


def test_update_source_health_normalizes_source_name_and_error(monkeypatch, tmp_path):
    health_path = tmp_path / "subscriptions_source_health.json"
    monkeypatch.setattr(runtime_health, "HEALTH_PATH", health_path)
    monkeypatch.setattr(views, "HEALTH_PATH", health_path)

    result = FeedFetchResult(
        source_id="wechat-source",
        source_name="观点 胖特工",
        feed_url="wechat://mp/demo",
        ok=False,
        status=500,
        entries=[],
        error="登录态 已过期",
    )

    runtime_health.update_source_health(result)
    row = views.load_health()["sources"]["wechat-source"]

    assert row["source_name"] == "观点 胖特工"
    assert row["last_error"] == "登录态 已过期"


def test_run_source_fetch_updates_status_and_health(monkeypatch, tmp_path):
    health_path = tmp_path / "subscriptions_source_health.json"
    status_path = tmp_path / "subscriptions_status.json"
    monkeypatch.setattr(runtime_health, "HEALTH_PATH", health_path)
    monkeypatch.setattr(runtime_health, "STATUS_PATH", status_path)
    monkeypatch.setattr(views, "HEALTH_PATH", health_path)
    monkeypatch.setattr(views, "STATUS_PATH", status_path)

    result = FeedFetchResult(
        source_id="alphapai",
        source_name="新闻 Alpha派蓝宝书",
        feed_url="https://alphapai-web.rabyte.cn/reading/home/market-report/detail",
        ok=True,
        status=200,
        entries=[],
    )

    monkeypatch.setattr(runtime_health, "fetch_many", lambda sources, settings=None, timeout=45, session=None: [result])
    monkeypatch.setattr(runtime_health, "save_entries", lambda entries: 0)

    outcome = runtime_health.run_source_fetch([{"id": "alphapai", "enabled": True}], timeout=5)

    assert outcome.success_sources == 1
    assert outcome.status["fetch_state"] == "success"
    assert outcome.status["last_success_sources"] == 1
    assert views.load_health()["sources"]["alphapai"]["source_name"] == "新闻 Alpha派蓝宝书"


def test_save_and_list_entries_normalize_chinese(monkeypatch, tmp_path):
    db_path = tmp_path / "subscriptions.sqlite3"
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", db_path)

    rss_db.save_entries(
        [
            FeedEntry(
                source_id="wechat-pangtegong",
                source_name="观点 胖特工",
                title="今日宏观",
                link="https://example.com/a",
                published="2026-05-22 06:00:00",
                summary="测试摘要",
            )
        ]
    )

    row = rss_db.list_entries()[0]
    assert row["source_name"] == "观点 胖特工"
    assert row["title"] == "今日宏观"
    assert row["summary"] == "测试摘要"

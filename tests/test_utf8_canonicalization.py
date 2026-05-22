from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from apps.subscriptions import rss_db
from apps.subscriptions import rss_config
from scripts import normalize_runtime_utf8
from web.services import fetch_runtime, views


def test_update_source_health_normalizes_source_name_and_error(monkeypatch, tmp_path):
    health_path = tmp_path / "subscriptions_source_health.json"
    monkeypatch.setattr(fetch_runtime, "HEALTH_PATH", health_path)
    monkeypatch.setattr(views, "HEALTH_PATH", health_path)

    result = FeedFetchResult(
        source_id="wechat-source",
        source_name="è§ç¹ èç¹å·¥",
        feed_url="wechat://mp/demo",
        ok=False,
        status=500,
        entries=[],
        error="ç»å½æ å·²è¿æ",
    )

    fetch_runtime.update_source_health(result)
    payload = views.load_health()
    row = payload["sources"]["wechat-source"]

    assert row["source_name"] == "观点 胖特工"
    assert row["last_error"] == "登录态 已过期"


def test_save_and_list_entries_normalize_chinese(monkeypatch, tmp_path):
    db_path = tmp_path / "subscriptions.sqlite3"
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", db_path)

    rss_db.save_entries(
        [
            FeedEntry(
                source_id="wechat-pangtegong",
                source_name="è§ç¹ èç¹å·¥",
                title="ä»æ¥å®è§",
                link="https://example.com/a",
                published="2026-05-22 06:00:00",
                summary="æµè¯æè¦",
            )
        ]
    )

    rows = rss_db.list_entries()
    assert rows[0]["source_name"] == "观点 胖特工"
    assert rows[0]["title"] == "今日宏观"
    assert rows[0]["summary"] == "测试摘要"


def test_normalize_runtime_script_repairs_target_files(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    health_dir = runtime_dir / "health"
    auth_dir = runtime_dir / "auth"
    config_dir = tmp_path / "config"
    health_dir.mkdir(parents=True)
    auth_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)

    legacy_auth = runtime_dir / "wechat_auth.json"
    canonical_auth = auth_dir / "wechat_mp_main.json"
    automation = health_dir / "automation_runtime.json"
    health = health_dir / "subscriptions_source_health.json"
    sources = config_dir / "subscription_sources.json"

    legacy_auth.write_text('{"nickname":"èè´¦å·"}\n', encoding="utf-8")
    canonical_auth.write_text('{"nickname":"ä¸»è´¦å·"}\n', encoding="utf-8")
    automation.write_text('{"slots":{"daily_0600":{"label":"æ¯æ¥ 06:00 è®¢é+ç¨åè¯»"}}}\n', encoding="utf-8")
    health.write_text(
        '{"sources":{"wechat-pangtegong":{"last_checked_at":"2026-05-22 06:01:51","last_success_at":"","last_failed_at":"","last_error":"ç»å½æ å·²è¿æ","source_name":"è§ç¹ èç¹å·¥"}}}\n',
        encoding="utf-8",
    )
    sources.write_text(
        '{"sources":[{"id":"wechat-pangtegong","name":"è§ç¹ èç¹å·¥","group":"","feed_url":"wechat://mp/demo","site_url":"wechat://mp/demo","provider":"web","fetch_via":"wechat-api","kind":"web","enabled":true,"note":"","channel":"wechat","auth_key":"wechat_mp_main","fallback_mode":"none"}]}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(normalize_runtime_utf8, "BASE_DIR", tmp_path)
    monkeypatch.setattr(normalize_runtime_utf8, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(normalize_runtime_utf8, "HEALTH_DIR", health_dir)
    monkeypatch.setattr(rss_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(rss_config, "SOURCES_PATH", sources)
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(rss_db, "DB_PATH", tmp_path / "data" / "subscriptions.sqlite3")
    monkeypatch.setattr(views, "HEALTH_PATH", health)
    monkeypatch.setattr(views, "STATUS_PATH", health_dir / "subscriptions_status.json")
    monkeypatch.setattr(views, "RUNTIME_DIR", runtime_dir)

    normalize_runtime_utf8.main()

    assert "老账号" in legacy_auth.read_text(encoding="utf-8")
    assert "主账号" in canonical_auth.read_text(encoding="utf-8")
    assert "每日 06:00 订阅+稍后读" in automation.read_text(encoding="utf-8")
    assert "观点 胖特工" in health.read_text(encoding="utf-8")
    assert "登录态 已过期" in health.read_text(encoding="utf-8")
    assert "观点 胖特工" in sources.read_text(encoding="utf-8")

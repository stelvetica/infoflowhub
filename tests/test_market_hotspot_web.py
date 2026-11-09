"""市场热点产物登记进新闻面板白名单的行为检查。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from apps.subscriptions import rss_db
from web.services import views

TODAY = date.today().isoformat()
MD_NAME = f"市场热点_{TODAY}.md"
JSON_NAME = f"市场热点底稿_{TODAY}.json"
SAMPLE_MD = "# 市场热点\n\n| 排名 | 行业 |\n| --- | --- |\n| 1 | 半导体 |\n"


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", tmp_path / "subscriptions.sqlite3")
    return tmp_path


@pytest.fixture()
def reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True)
    (reports / MD_NAME).write_text(SAMPLE_MD, encoding="utf-8")
    (reports / JSON_NAME).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(views, "REPORTS_DIR", reports)
    return reports


def test_report_patterns_registers_market_hotspot():
    assert ("市场热点_*.md", "市场热点") in views.REPORT_PATTERNS


def test_all_scope_lists_market_hotspot_md_only(reports_dir: Path, temp_db: Path):
    view = views.get_news_view({"news_scope": "all"})
    assert MD_NAME in {row["name"] for row in view["rows"]}
    assert JSON_NAME not in {row["name"] for row in view["rows"]}
    kinds = {row["kind"] for row in view["rows"]}
    assert "市场热点" in kinds


def test_flow_scope_lists_today_market_hotspot(reports_dir: Path, temp_db: Path):
    # 流程范围按文件名日期=今天判定，市场热点不依赖 mtime
    view = views.get_news_view({"news_scope": "flow"})
    assert MD_NAME in {row["name"] for row in view["rows"]}
    assert JSON_NAME not in {row["name"] for row in view["rows"]}


def test_json_draft_never_appears(reports_dir: Path, temp_db: Path):
    for scope in ("all", "flow"):
        names = {row["name"] for row in views.get_news_view({"news_scope": scope})["rows"]}
        assert JSON_NAME not in names

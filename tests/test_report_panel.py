"""「新闻」面板改为展示 data/reports 生成产物后的行为检查（含路由防穿越）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from apps.subscriptions import rss_db
from apps.subscriptions.models import FeedEntry
from web.app import app
from web.routes import main
from web.services import views
from web.services.utils import read_entry_key, read_link_key

REPORT_MD = "新闻影响分析_2026-09-20.md"
ANNOUNCE_MD = "公告清单_2026-09-20.md"
ANNOUNCE_PNG = "公告清单_2026-09-20.png"
NOISE_MD = "今日新闻.md"
NOISE_JSON = "used_news.json"
SAMPLE_MD = "# 标题\n\n| 项目 | 值 |\n| --- | --- |\n| A | 1 |\n"


def _write_reports_dir(tmp_path: Path) -> Path:
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True)
    (reports / REPORT_MD).write_text(SAMPLE_MD, encoding="utf-8")
    (reports / ANNOUNCE_MD).write_text(SAMPLE_MD, encoding="utf-8")
    (reports / ANNOUNCE_PNG).write_bytes(b"\x89PNG\r\n\x1a\n")
    (reports / NOISE_MD).write_text(SAMPLE_MD, encoding="utf-8")
    (reports / NOISE_JSON).write_text("{}", encoding="utf-8")
    return reports


@pytest.fixture()
def reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reports = _write_reports_dir(tmp_path)
    monkeypatch.setattr(views, "REPORTS_DIR", reports)
    monkeypatch.setattr(main, "REPORTS_DIR", reports)
    return reports


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把订阅库挪到 tmp，避免测试读写真实 data/subscriptions.sqlite3。"""
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", tmp_path / "subscriptions.sqlite3")
    return tmp_path / "subscriptions.sqlite3"


@pytest.fixture()
def client(reports_dir: Path) -> TestClient:
    # 不用 with 进入上下文：避免触发 lifespan（AutoRunner 后台任务）
    return TestClient(app)


# ── 新闻面板数据源 ──────────────────────────────────────────────


def test_news_view_only_lists_whitelisted_reports(reports_dir: Path, temp_db: Path):
    view = views.get_news_view({})
    assert view["filtered_total"] == 3
    assert {row["kind"] for row in view["rows"]} == {"新闻影响分析", "公告清单", "公告图片"}
    assert {row["name"] for row in view["rows"]} == {REPORT_MD, ANNOUNCE_MD, ANNOUNCE_PNG}
    assert {row["read_key"] for row in view["rows"]} == {f"report:{name}" for name in (REPORT_MD, ANNOUNCE_MD, ANNOUNCE_PNG)}
    assert all(row["link"].startswith("/reports/view/") for row in view["rows"])


def test_news_view_returns_empty_when_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db: Path):
    monkeypatch.setattr(views, "REPORTS_DIR", tmp_path / "not-there")
    view = views.get_news_view({})
    assert view["filtered_total"] == 0
    assert view["rows"] == []


def test_news_view_ignores_entries_read_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db: Path):
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True)
    (reports / "新闻影响分析_2026-01-05.md").write_text("# 旧产物\n", encoding="utf-8")
    monkeypatch.setattr(views, "REPORTS_DIR", reports)
    # 早于 ENTRIES_READ_CUTOFF 的产物也必须照常出现
    assert views.get_news_view({"news_unread_only": "1"})["filtered_total"] == 1


def test_news_view_supports_kind_sort_and_title_search(reports_dir: Path, temp_db: Path):
    kinds = [row["kind"] for row in views.get_news_view({"news_sort": "kind", "news_dir": "asc"})["rows"]]
    assert kinds == sorted(kinds)
    assert views.get_news_view({"news_q": "公告"})["filtered_total"] == 2


# ── 产物路由 ────────────────────────────────────────────────────


def test_report_view_renders_markdown_as_html(client: TestClient):
    response = client.get(f"/reports/view/{quote(REPORT_MD, safe='')}")
    assert response.status_code == 200
    assert "<table" in response.text
    assert "<h1>标题</h1>" in response.text


def test_report_raw_markdown_is_plain_text(client: TestClient):
    response = client.get(f"/reports/raw/{quote(REPORT_MD, safe='')}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("# 标题")


def test_report_raw_png_is_image(client: TestClient):
    response = client.get(f"/reports/raw/{quote(ANNOUNCE_PNG, safe='')}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")


def test_report_view_png_redirects_to_raw(client: TestClient):
    response = client.get(f"/reports/view/{quote(ANNOUNCE_PNG, safe='')}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/reports/raw/{quote(ANNOUNCE_PNG, safe='')}"


def test_markdown_route_serves_local_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient):
    root = tmp_path / "jiuyangongshe_markdown"
    root.mkdir()
    (root / "2026-09-20_文章.md").write_text("# 正文\n", encoding="utf-8")
    monkeypatch.setattr(main, "MARKDOWN_DIRS", {**main.MARKDOWN_DIRS, "jiuyangongshe": root})
    response = client.get("/markdown/jiuyangongshe/" + quote("2026-09-20_文章.md", safe=""))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "正文" in response.text


@pytest.mark.parametrize(
    "url",
    [
        "/reports/view/../requirements.txt",
        "/reports/view/%2e%2e%2frequirements.txt",
        "/markdown/alphapai/..%2f..%2f.env",
        "/markdown/unknown/x.md",
    ],
)
def test_traversal_and_unknown_kind_return_404(client: TestClient, url: str):
    assert client.get(url).status_code == 404


def test_resolve_inside_rejects_traversal_and_suffix(tmp_path: Path):
    root = tmp_path / "reports"
    root.mkdir()
    (root / "ok.md").write_text("# ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    assert main._resolve_inside(root, "ok.md", (".md",)) == (root / "ok.md").resolve()
    assert main._resolve_inside(root, "../secret.txt", (".md", ".txt")) is None
    assert main._resolve_inside(root, "..\\secret.txt", (".md", ".txt")) is None
    assert main._resolve_inside(root, "ok.md", (".png",)) is None
    assert main._resolve_inside(root, "missing.md", (".md",)) is None
    assert main._resolve_inside(tmp_path / "not-there", "ok.md", (".md",)) is None


# ── 条目 link 改写与 read_key 顺序 ──────────────────────────────


def test_markdown_url_mapping():
    assert views.markdown_url("alphapai", "C:/x/data/alphapai_markdown/a b.md") == "/markdown/alphapai/a%20b.md"
    assert views.markdown_url("jiuyangongshe_jiujiubashiyi", "/x/y.md") == "/markdown/jiuyangongshe/y.md"
    assert views.markdown_url("路口大爷聊宏观", "/x/y.md") == ""
    assert views.markdown_url("alphapai", "") == ""


def test_build_entry_row_keeps_read_key_on_original_link():
    original_link = "https://www.jiuyangongshe.com/a/xxx"
    item: dict[str, Any] = {
        "source_id": "jiuyangongshe_jiucaituanzi",
        "source_name": "资讯 韭菜团子",
        "title": "某文章",
        "link": original_link,
        "published_at": "2026-09-20 08:00:00",
        "summary": "",
        "markdown_path": str(Path("D:/data/jiuyangongshe_markdown") / "2026-09-20_某文章.md"),
    }
    row = views._build_entry_row(item, {}, {})
    assert row["read_key"] == read_entry_key("jiuyangongshe_jiucaituanzi", original_link, "某文章")
    assert row["read_key"] == read_link_key(original_link)
    assert row["link"] == "/markdown/jiuyangongshe/" + quote("2026-09-20_某文章.md", safe="")
    assert row["read_key"] != read_link_key(row["link"])
    assert row["has_link"] is True


def test_build_entry_row_alphapai_read_key_uses_title():
    item: dict[str, Any] = {
        "source_id": "alphapai",
        "source_name": "资讯 Alpha派蓝宝书",
        "title": "蓝宝书标题",
        "link": "https://alphapai-web.rabyte.cn/reading/home/market-report/detail?id=1",
        "published_at": "2026-09-20 20:00:00",
        "summary": "",
        "markdown_path": "C:/x/data/alphapai_markdown/2026-09-20_蓝宝书标题_ab12.md",
    }
    row = views._build_entry_row(item, {}, {})
    assert row["read_key"] == read_link_key("alphapai|蓝宝书标题")
    assert row["link"] == "/markdown/alphapai/2026-09-20_%E8%93%9D%E5%AE%9D%E4%B9%A6%E6%A0%87%E9%A2%98_ab12.md"


def test_build_entry_row_without_markdown_keeps_original_link():
    item: dict[str, Any] = {
        "source_id": "路口大爷聊宏观",
        "source_name": "观点 路口大爷聊宏观",
        "title": "某观点",
        "link": "https://example.com/a",
        "published_at": "2026-09-20 08:00:00",
        "summary": "",
        "markdown_path": "",
    }
    row = views._build_entry_row(item, {}, {})
    assert row["link"] == "https://example.com/a"
    assert row["read_key"] == read_link_key("https://example.com/a")


# ── 订阅面板不再排除「资讯」前缀源 ──────────────────────────────


def _source(source_id: str, name: str) -> dict[str, Any]:
    return {
        "id": source_id,
        "name": name,
        "group": "",
        "feed_url": f"https://example.com/{source_id}.xml",
        "site_url": "",
        "provider": "native",
        "fetch_via": "direct",
        "kind": "rss",
        "enabled": True,
        "note": "",
        "channel": "rss",
        "auth_key": "",
        "fallback_mode": "none",
    }


def test_entries_view_includes_news_prefixed_sources(temp_db: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(views, "load_sources", lambda: [_source("alpha-news", "资讯 某蓝宝书"), _source("opinion", "观点 某博主")])
    rss_db.save_entries(
        [
            FeedEntry(source_id="alpha-news", source_name="资讯 某蓝宝书", title="资讯条目", link="https://example.com/n1", published="2026/09/20 08:00", summary=""),
            FeedEntry(source_id="opinion", source_name="观点 某博主", title="观点条目", link="https://example.com/o1", published="2026/09/20 09:00", summary=""),
        ]
    )
    titles = [row["title"] for row in views.get_entries_view({})["rows"]]
    assert "资讯条目" in titles
    assert "观点条目" in titles
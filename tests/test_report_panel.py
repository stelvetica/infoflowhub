"""「新闻」面板改为展示 data/reports 生成产物后的行为检查（含路由防穿越）。"""

from __future__ import annotations

import json
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
from web.services.utils import read_entry_key, read_link_key, split_read_keys, split_tags

REPORT_MD = "新闻影响分析_2026-09-20.md"
ANNOUNCE_MD = "公告清单_2026-09-20.md"
ANNOUNCE_PNG = "公告清单_2026-09-20.png"
# 流程中间产物：本次需求有意纳入白名单（用户要求流程中间产物也要能看到）
TODAY_NEWS_MD = "今日新闻.md"
NOISE_JSON = "used_news.json"
SAMPLE_MD = "# 标题\n\n| 项目 | 值 |\n| --- | --- |\n| A | 1 |\n"


def _write_reports_dir(tmp_path: Path) -> Path:
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True)
    (reports / REPORT_MD).write_text(SAMPLE_MD, encoding="utf-8")
    (reports / ANNOUNCE_MD).write_text(SAMPLE_MD, encoding="utf-8")
    (reports / ANNOUNCE_PNG).write_bytes(b"\x89PNG\r\n\x1a\n")
    (reports / TODAY_NEWS_MD).write_text(SAMPLE_MD, encoding="utf-8")
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


FLOW_STARTED = "2026-09-22 08:10:00"


@pytest.fixture()
def flow_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把流程状态文件挪到 tmp，避免读写真实 runtime/health/automation_runtime.json。"""
    runtime = tmp_path / "runtime" / "automation_runtime.json"
    monkeypatch.setattr(views, "AUTOMATION_RUNTIME_PATH", runtime)
    return runtime


def _write_flow(runtime: Path, flow: dict | None) -> None:
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(json.dumps({"flow": flow} if flow else {}), encoding="utf-8")


# ── 新闻面板数据源 ──────────────────────────────────────────────


def test_news_view_only_lists_whitelisted_reports(reports_dir: Path, temp_db: Path):
    # 用 all 范围断言白名单全集（flow 范围是最近一次流程的子集，另见 test_news_flow_snapshot.py）
    view = views.get_news_view({"news_scope": "all"})
    assert view["filtered_total"] == 4
    assert {row["kind"] for row in view["rows"]} == {"新闻影响分析", "公告清单", "公告图片", "今日热点"}
    assert {row["name"] for row in view["rows"]} == {REPORT_MD, ANNOUNCE_MD, ANNOUNCE_PNG, TODAY_NEWS_MD}
    assert {row["read_key"] for row in view["rows"]} == {f"report:{name}" for name in (REPORT_MD, ANNOUNCE_MD, ANNOUNCE_PNG, TODAY_NEWS_MD)}
    assert all(row["link"].startswith("/reports/view/") for row in view["rows"])


def test_news_view_returns_empty_when_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db: Path):
    monkeypatch.setattr(views, "REPORTS_DIR", tmp_path / "not-there")
    view = views.get_news_view({"news_scope": "all"})
    assert view["filtered_total"] == 0
    assert view["rows"] == []


def test_news_view_ignores_entries_read_cutoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db: Path):
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True)
    (reports / "新闻影响分析_2026-01-05.md").write_text("# 旧产物\n", encoding="utf-8")
    monkeypatch.setattr(views, "REPORTS_DIR", reports)
    # 早于 ENTRIES_READ_CUTOFF 的产物也必须照常出现
    assert views.get_news_view({"news_unread_only": "1", "news_scope": "all"})["filtered_total"] == 1


def test_news_view_supports_kind_sort_and_title_search(reports_dir: Path, temp_db: Path):
    kinds = [row["kind"] for row in views.get_news_view({"news_sort": "kind", "news_dir": "asc", "news_scope": "all"})["rows"]]
    assert kinds == sorted(kinds)
    assert views.get_news_view({"news_q": "公告", "news_scope": "all"})["filtered_total"] == 2


def test_news_read_keys_keep_colon_intact(reports_dir: Path, temp_db: Path):
    """read_key 是 report:文件名.md 这样的内部机器值，宽松分割会把冒号拆成脏键。"""
    key = f"report:{REPORT_MD}"
    # split_tags 会把冒号当分隔符（标签场景有意为之），read_key 不能复用它
    assert len(split_tags(key)) > 1
    assert split_read_keys(key) == [key]
    assert split_read_keys(f"abc123,{key}") == ["abc123", key]
    # 片段回传的 data-read-keys 必须是原样的 key，不能混进 report / 文件名 这类脏键
    view = views.get_news_view({"news_read_keys": key, "news_scope": "all"})
    assert key in view["read_keys_text"].split(",")
    assert "report" not in view["read_keys_text"].split(",")
    assert REPORT_MD not in view["read_keys_text"].split(",")


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
    # NUL 等控制字符：resolve 会抛 ValueError，必须判非法返回 None 而非 500
    assert main._resolve_inside(root, "ok\x00.md", (".md",)) is None
    assert main._resolve_inside(root, "a\nb.md", (".md",)) is None


@pytest.mark.parametrize("url", ["/fragments/news-report/%00.md", "/reports/raw/%00.md", "/reports/view/%00.md"])
def test_nul_byte_name_returns_404_not_500(client: TestClient, url: str):
    assert client.get(url).status_code == 404


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


# ── 范围切换（news_scope）──────────────────────────────────────


def test_build_news_state_params_defaults_to_flow_scope():
    assert views.build_news_state_params({})["news_scope"] == "flow"
    assert views.build_news_state_params({"news_scope": "all"})["news_scope"] == "all"
    # 非法值一律回落到 flow，避免 cookie 里塞进奇怪取值
    assert views.build_news_state_params({"news_scope": "bogus"})["news_scope"] == "flow"


def test_news_scope_flow_lists_only_current_flow_products(reports_dir: Path, flow_state: Path, temp_db: Path):
    _write_flow(
        flow_state,
        {
            "flow_id": "20260922-081000",
            "status": "success",
            "products": [{"name": REPORT_MD, "kind": "新闻影响分析", "is_image": False}],
        },
    )
    view = views.get_news_view({"news_scope": "flow"})
    assert view["scope"] == "flow"
    assert [row["name"] for row in view["rows"]] == [REPORT_MD]
    # 同一份产物目录，all 范围仍返回整个白名单
    all_view = views.get_news_view({"news_scope": "all"})
    assert all_view["scope"] == "all"
    assert all_view["filtered_total"] == 4


def test_news_scope_flow_empty_without_flow_record(reports_dir: Path, flow_state: Path, temp_db: Path):
    _write_flow(flow_state, None)
    assert views.get_flow_state() == {}
    assert views.get_news_view({"news_scope": "flow"})["filtered_total"] == 0


# ── 内联内容片段路由 ────────────────────────────────────────────


def test_news_report_fragment_renders_markdown(client: TestClient):
    response = client.get(f"/fragments/news-report/{quote(REPORT_MD, safe='')}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>标题</h1>" in response.text
    assert "<table" in response.text
    # 保留原有能力：内容区提供新页面打开入口
    assert f"/reports/view/{quote(REPORT_MD, safe='')}" in response.text


def test_news_report_fragment_renders_png_as_img(client: TestClient):
    response = client.get(f"/fragments/news-report/{quote(ANNOUNCE_PNG, safe='')}")
    assert response.status_code == 200
    assert f'<img class="news-report-image" src="/reports/raw/{quote(ANNOUNCE_PNG, safe="")}"' in response.text


@pytest.mark.parametrize(
    "name",
    [
        "missing.md",
        NOISE_JSON,  # 后缀不在白名单
        "..%2frequirements.txt",
        "%2e%2e%2frequirements.txt",
    ],
)
def test_news_report_fragment_rejects_unknown_and_traversal(client: TestClient, name: str):
    assert client.get(f"/fragments/news-report/{name}").status_code == 404


def test_news_panel_exposes_scope_and_flow_empty_text(client: TestClient, flow_state: Path, temp_db: Path):
    _write_flow(flow_state, {"flow_id": "x", "status": "running", "skills": [], "products": []})
    response = client.get("/fragments/news")
    assert response.status_code == 200
    assert 'data-news-scope="flow"' in response.text
    assert "本次流程暂无新增产物" in response.text


def test_news_scope_switch_via_route(client: TestClient, reports_dir: Path, flow_state: Path, temp_db: Path):
    _write_flow(
        flow_state,
        {"flow_id": "x", "status": "success", "products": [{"name": REPORT_MD, "kind": "新闻影响分析", "is_image": False}]},
    )
    flow_resp = client.get("/fragments/news?news_scope=flow")
    assert 'data-news-scope="flow"' in flow_resp.text
    assert REPORT_MD in flow_resp.text
    assert ANNOUNCE_MD not in flow_resp.text

    all_resp = client.get("/fragments/news?news_scope=all")
    assert 'data-news-scope="all"' in all_resp.text
    assert all(name in all_resp.text for name in (REPORT_MD, ANNOUNCE_MD, ANNOUNCE_PNG, TODAY_NEWS_MD))
    # 范围链接/排序链接都只带一次 news_scope，重复取值会依赖解析顺序
    assert "news_scope=all&amp;news_scope" not in all_resp.text


# ── 流程状态片段 ────────────────────────────────────────────────


def test_news_flow_status_running_has_polling(client: TestClient, flow_state: Path):
    _write_flow(
        flow_state,
        {"flow_id": "20260922-081000", "status": "running", "started_at": FLOW_STARTED, "skills": [], "products": []},
    )
    response = client.get("/fragments/news-flow-status")
    assert response.status_code == 200
    assert 'hx-trigger="every 10s"' in response.text
    assert "流程运行中" in response.text
    assert "抓取订阅与稍后处理" in response.text


def test_news_flow_status_terminal_has_no_polling(client: TestClient, flow_state: Path):
    _write_flow(
        flow_state,
        {
            "flow_id": "20260922-081000",
            "status": "partial",
            "started_at": FLOW_STARTED,
            "finished_at": "2026-09-22 08:20:00",
            "skills": [{"name": "portfolio-news", "status": "error", "error": "boom"}],
            "products": [{"name": REPORT_MD, "kind": "新闻影响分析", "is_image": False}],
        },
    )
    response = client.get("/fragments/news-flow-status")
    assert "hx-trigger" not in response.text
    assert "部分失败" in response.text
    assert "新增产物 1 个" in response.text
    assert "portfolio-news" in response.text
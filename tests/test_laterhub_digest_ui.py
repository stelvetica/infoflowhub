"""稍后读「解析」按钮：数据上下文、模板三态、领取路由与报告查看。

不触网络/不跑 ASR：后台任务在路由里被替换为记录器，只验证接线与状态流转。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

import web.routes.main as main
import web.services.views as views
from apps.laterhub.db import DBManager, LinkRecord, is_digest_video_url
from web.app import app

BASE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = BASE_DIR / "web" / "templates"
BILI = "https://www.bilibili.com/video/BV1xx411c7mD"
DOUYIN = "https://www.douyin.com/video/7123456789"
ARTICLE = "https://mp.weixin.qq.com/s/abcdefg"


def _set_digest(db_path: Path, link_id: int, **values: object) -> None:
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"UPDATE links SET {assignments} WHERE id = ?", (*values.values(), link_id))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "laterhub.sqlite3"
    monkeypatch.setattr(views, "LATERHUB_DB_PATH", path)
    monkeypatch.setattr(main, "DB_PATH", path)
    db = DBManager(path)
    pending = db.upsert_link(LinkRecord(url=BILI, title="待解析视频", source="bilibili_watchlater"))
    done = db.upsert_link(LinkRecord(url=DOUYIN, title="已解析视频", source="douyin_favorites"))
    article = db.upsert_link(LinkRecord(url=ARTICLE, title="普通文章", source="manual_verify"))
    _set_digest(path, done, digest_status="succeeded", digest_path="video_digest/已解析视频.md", digest_at="2026-11-04T09:00:00")
    return path


@pytest.fixture()
def reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reports = tmp_path / "reports" / "video_digest"
    reports.mkdir(parents=True)
    (reports / "已解析视频.md").write_text("# 已解析视频\n\n## 一句话总结\n\n测试报告。\n", encoding="utf-8")
    monkeypatch.setattr(main, "DIGEST_REPORTS_DIR", reports)
    return reports


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _row_by_title(title: str) -> dict:
    rows, _, _ = views._build_laterhub_rows()
    return next(row for row in rows if row["title"] == title)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://m.bilibili.com/video/BV1xx411c7mD?p=2",
        "https://www.douyin.com/video/7123456789",
        "https://m.douyin.com/video/7123456789",
    ],
)
def test_digest_video_url_accepts_real_video_pages(url: str) -> None:
    assert is_digest_video_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://mp.weixin.qq.com/s/abcdefg",
        "https://www.xiaoheihe.cn/app/bbs/link/123",
        "https://weread.qq.com/web/bookDetail/abc",
        "https://www.bilibili.com/bangumi/play/ep123",  # B站但非 /video/
        "https://t.bilibili.com/123456",  # 动态
        "https://evil.example/?next=https://www.bilibili.com/video/BV1xx411c7mD",  # 子串陷阱
        "https://notbilibili.com/video/BV1xx411c7mD",  # 相似域名
        "",
        "not-a-url",
    ],
)
def test_digest_video_url_rejects_non_video(url: str) -> None:
    assert is_digest_video_url(url) is False


def test_rows_expose_video_and_digest_fields(db_path: Path) -> None:
    pending = _row_by_title("待解析视频")
    assert pending["is_video"] is True
    assert pending["digest_status"] == "pending"
    assert pending["digest_name"] == ""

    done = _row_by_title("已解析视频")
    assert done["is_video"] is True
    assert done["digest_status"] == "succeeded"
    assert done["digest_name"] == "已解析视频.md"

    article = _row_by_title("普通文章")
    assert article["is_video"] is False


def test_digest_cell_renders_gray_running_and_blue_states() -> None:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))
    template = env.get_template("partials/laterhub_digest_cell.html")

    pending = template.render(item={"id": 7, "is_video": True, "digest_status": "pending", "digest_name": "", "digest_error": ""})
    assert "btn ghost" in pending and "解析" in pending
    assert 'data-digest-url="/actions/laterhub/7/digest"' in pending
    assert "digest-done" not in pending

    running = template.render(item={"id": 7, "is_video": True, "digest_status": "running", "digest_name": "", "digest_error": ""})
    assert "解析中" in running and "disabled" in running
    assert "data-digest-url" not in running

    succeeded = template.render(
        item={"id": 7, "is_video": True, "digest_status": "succeeded", "digest_name": "报告 1.md", "digest_error": ""}
    )
    assert 'class="btn digest-done"' in succeeded
    assert "/digest/view/%E6%8A%A5%E5%91%8A%201.md" in succeeded
    assert "已解析" in succeeded

    # 非视频不渲染按钮，避免给人「能解析」的错觉
    article = template.render(item={"id": 7, "is_video": False, "digest_status": "pending", "digest_name": "", "digest_error": ""})
    assert article.strip() == ""


def test_start_digest_claims_once_and_reports_running(
    db_path: Path, reports_dir: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(main, "_digest_job", lambda link_id: calls.append(link_id))
    link_id = 1  # fixture 的第一条 = 待解析 B站视频

    first = client.post(f"/actions/laterhub/{link_id}/digest")
    assert first.status_code == 200
    assert "解析中" in first.text
    assert _status(db_path, link_id) == "running"

    # 已在 running 时再点不会重复起任务
    second = client.post(f"/actions/laterhub/{link_id}/digest")
    assert second.status_code == 200
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls == [link_id]


def test_start_digest_rejects_non_video(db_path: Path, reports_dir: Path, client: TestClient) -> None:
    response = client.post("/actions/laterhub/3/digest")
    assert response.status_code == 400


def test_digest_fragment_and_report_view(db_path: Path, reports_dir: Path, client: TestClient) -> None:
    fragment = client.get("/fragments/laterhub/2/digest")
    assert fragment.status_code == 200
    assert "已解析" in fragment.text

    report = client.get("/digest/view/已解析视频.md")
    assert report.status_code == 200
    assert "测试报告" in report.text

    assert client.get("/digest/view/不存在.md").status_code == 404


def _status(db_path: Path, link_id: int) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return str(conn.execute("SELECT digest_status FROM links WHERE id = ?", (link_id,)).fetchone()[0])
    finally:
        conn.close()

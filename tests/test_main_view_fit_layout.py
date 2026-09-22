"""主视图锁高开关的模板检查:主视图贴合视口(表格内滚、分页常驻视野),设置页照常整页滚动。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.subscriptions import rss_db
from web.app import app
from web.services import views


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # 订阅库与产物目录挪到 tmp,避免测试读写真实数据;不用 with 进入上下文以免触发 lifespan
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", tmp_path / "subscriptions.sqlite3")
    monkeypatch.setattr(views, "REPORTS_DIR", tmp_path / "data" / "reports")
    # 旧源 id 迁移要读 rss_source_state(由抓取器建表),空库下会报错,与本次布局检查无关
    monkeypatch.setattr(views, "migrate_legacy_source_ids", lambda: 0)
    return TestClient(app)


def test_main_view_locks_height_to_viewport(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'class="main main-fit"' in response.text


def test_settings_view_keeps_page_scroll(client: TestClient) -> None:
    response = client.get("/?view=settings")

    assert response.status_code == 200
    assert "main-fit" not in response.text
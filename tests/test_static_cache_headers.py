"""静态资源必须走条件校验：页面 HTML 每次新鲜，CSS/JS 一旦被浏览器无校验复用旧版，
就会出现「新 HTML 配旧 CSS」的布局错乱（见 web/app.py 中 StaticFilesNoCache 的说明）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web.app import app

ASSETS = ["/static/styles.css", "/static/app.js"]


@pytest.fixture()
def client() -> TestClient:
    # 不用 with 进入上下文：避免触发 lifespan（AutoRunner 后台任务）
    return TestClient(app)


@pytest.mark.parametrize("asset", ASSETS)
def test_asset_must_be_revalidated(client: TestClient, asset: str) -> None:
    response = client.get(asset)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]


@pytest.mark.parametrize("asset", ASSETS)
def test_unchanged_asset_returns_304(client: TestClient, asset: str) -> None:
    etag = client.get(asset).headers["etag"]

    response = client.get(asset, headers={"if-none-match": etag})

    assert response.status_code == 304
    assert response.headers["cache-control"] == "no-cache"
"""快速收录的图片分支：文件名=本地日期时间、内容嗅探、附件路由与面板标记。"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

import pytest

import web.routes.main as main_routes
import web.services.views as views

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
NAME_RE = re.compile(r"^\d{8}-\d{6}\.png$")
TITLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    db_path = tmp_path / "laterhub.sqlite3"
    attachments = tmp_path / "laterhub_attachments"
    monkeypatch.setattr(main_routes, "DB_PATH", db_path)
    monkeypatch.setattr(main_routes, "ATTACHMENTS_DIR", attachments)
    monkeypatch.setattr(views, "LATERHUB_DB_PATH", db_path)
    return db_path, attachments


def test_image_named_by_local_datetime(paths):
    db_path, attachments = paths

    result = main_routes.save_laterhub_image(PNG)

    name = result["url"].removeprefix("/attachments/")
    assert NAME_RE.match(name), name
    assert (attachments / name).read_bytes() == PNG
    assert TITLE_RE.match(result["title"]), result["title"]
    with sqlite3.connect(db_path) as conn:
        url, title, source, tag_status, tags = conn.execute(
            "SELECT url, title, source, tag_status, tags FROM links"
        ).fetchone()
    assert url == result["url"]
    assert title == result["title"]
    assert source == "manual_verify"
    # 图片没有可分类语义：直接标成已打标，避免占用每日 LLM 打标
    assert tag_status == "done"
    assert not tags


def test_same_second_images_get_suffix(paths, monkeypatch):
    _, attachments = paths

    class FixedDatetime:
        @classmethod
        def now(cls):
            return datetime(2026, 11, 10, 11, 30, 45)

    monkeypatch.setattr(main_routes, "datetime", FixedDatetime)

    first = main_routes.save_laterhub_image(PNG)
    second = main_routes.save_laterhub_image(PNG)

    assert first["url"] == "/attachments/20261110-113045.png"
    assert second["url"] == "/attachments/20261110-113045-2.png"
    assert len(list(attachments.iterdir())) == 2


def test_non_image_payload_rejected(paths):
    _, attachments = paths

    with pytest.raises(ValueError):
        main_routes.save_laterhub_image(b"<svg onload=alert(1)></svg>")
    assert not attachments.exists() or not any(attachments.iterdir())


def test_attachment_route_serves_only_whitelisted_files(paths):
    from fastapi.testclient import TestClient
    from web.app import app

    result = main_routes.save_laterhub_image(PNG)
    client = TestClient(app)  # 不进 with：不触发 lifespan

    response = client.get(result["url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG

    assert client.get("/attachments/missing.png").status_code == 404
    assert client.get("/attachments/evil.txt").status_code == 404


def test_panel_flags_images_but_not_notes(paths):
    main_routes.save_laterhub_image(PNG)
    main_routes.add_laterhub_note("普通文字")

    rows, _, _ = views._build_laterhub_rows()
    by_kind = {row["is_image"]: row for row in rows}

    assert by_kind[True]["is_note"] is False
    assert by_kind[False]["is_note"] is True


def test_upload_endpoint_accepts_and_rejects(paths):
    from fastapi.testclient import TestClient
    from web.app import app

    client = TestClient(app)

    ok = client.post(
        "/actions/laterhub/add-image",
        files={"image": ("photo.png", PNG, "image/png")},
    )
    assert ok.status_code == 200
    assert ok.json()["kind"] == "image"

    bad = client.post(
        "/actions/laterhub/add-image",
        files={"image": ("note.txt", b"hello", "text/plain")},
    )
    assert bad.status_code == 400


def _encoded(fmt: str) -> bytes:
    from io import BytesIO

    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener()
    buf = BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()


def test_sniff_recognizes_mobile_formats():
    assert main_routes._sniff_image_suffix(b"\x00\x00\x00\x18ftypavif\x00\x00") == ".avif"
    assert main_routes._sniff_image_suffix(b"\x00\x00\x00\x18ftypheic\x00\x00") == ".heic"
    assert main_routes._sniff_image_suffix(b"\x00\x00\x00\x18ftypmif1\x00\x00") == ".heic"
    assert main_routes._sniff_image_suffix(b"BM" + b"0" * 20) == ".bmp"
    assert main_routes._sniff_image_suffix(b"II*\x00" + b"0" * 20) == ".tiff"


def test_heic_is_converted_to_jpeg(paths):
    _, attachments = paths

    result = main_routes.save_laterhub_image(_encoded("HEIF"))

    assert result["url"].endswith(".jpg")
    name = result["url"].removeprefix("/attachments/")
    assert (attachments / name).read_bytes().startswith(b"\xff\xd8\xff")


def test_tiff_is_converted_to_jpeg(paths):
    _, attachments = paths

    result = main_routes.save_laterhub_image(_encoded("TIFF"))

    assert result["url"].endswith(".jpg")
    name = result["url"].removeprefix("/attachments/")
    assert (attachments / name).read_bytes().startswith(b"\xff\xd8\xff")


def test_bmp_is_stored_without_conversion(paths):
    result = main_routes.save_laterhub_image(_encoded("BMP"))

    assert result["url"].endswith(".bmp")


def test_rejected_upload_logs_head(paths, caplog):
    from fastapi.testclient import TestClient
    from web.app import app

    client = TestClient(app)

    with caplog.at_level("WARNING", logger="web.routes.main"):
        response = client.post(
            "/actions/laterhub/add-image",
            files={"image": ("photo.unknown", b"\x00\x01\x02\x03" * 4, "application/octet-stream")},
        )

    assert response.status_code == 400
    assert "00010203" in caplog.text

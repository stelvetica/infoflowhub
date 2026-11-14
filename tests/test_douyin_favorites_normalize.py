"""收藏条目归一（图文 / 标题缺失兜底）、自带字幕标记解析、按标签选解析候选的回归。

不触网络、不开浏览器：只测纯函数与 sqlite。
"""

from __future__ import annotations

import base64
import json

from apps.laterhub.db import DBManager, LinkRecord
from connectors.douyin.favorites import normalize_douyin_item
from connectors.douyin.video import _subtitle_urls_from_marker

VIDEO_ID = "7412345678901234567"
NOTE_ID = "7412345678901234568"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"
NOTE_URL = f"https://www.douyin.com/note/{NOTE_ID}"


class FakePage:
    def __init__(self, html: str) -> None:
        self._html = html

    def content(self) -> str:
        return self._html


def _marker_html(entries: list[dict], current: dict | None = None) -> str:
    payload: dict = {"entries": entries}
    if current is not None:
        payload["current"] = current
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    packed = base64.b64encode(raw).decode()
    return f'<html><head><meta id="ifh-douyin-media" name="ifh-douyin-media" content="{packed}"></head></html>'


# ---------- 归一：标题缺失不丢条目 ----------

def test_normalize_keeps_item_when_title_missing():
    item = {"url": VIDEO_URL, "title": f"视频_{VIDEO_ID}", "raw_text": ""}
    result = normalize_douyin_item(item)
    assert result is not None
    assert VIDEO_ID in result["title"]


def test_normalize_uses_card_text_when_cover_alt_missing():
    item = {"url": VIDEO_URL, "title": f"视频_{VIDEO_ID}", "raw_text": "1.1万 三分钟讲透 RAG 检索增强 #AI"}
    result = normalize_douyin_item(item)
    assert result is not None
    assert result["title"] == "三分钟讲透 RAG 检索增强 #AI"


def test_normalize_accepts_note_url():
    result = normalize_douyin_item({"url": NOTE_URL, "title": "十条提示词模板", "raw_text": ""})
    assert result is not None
    assert result["url"] == NOTE_URL


def test_normalize_rejects_other_hosts():
    assert normalize_douyin_item({"url": "https://www.bilibili.com/video/BV1", "title": "x"}) is None


# ---------- 自带字幕标记 ----------

def test_subtitle_urls_prefers_chinese_and_filters_hosts():
    html = _marker_html([
        {
            "id": VIDEO_ID,
            "playUrls": ["https://v26-web.douyinvod.com/v/1"],
            "subtitleUrls": [
                "https://evil.example/sub.srt",
                "https://v26-web.douyinvod.com/sub/zh.srt",
            ],
        }
    ])
    assert _subtitle_urls_from_marker(FakePage(html), VIDEO_ID) == [
        "https://v26-web.douyinvod.com/sub/zh.srt"
    ]


def test_subtitle_urls_empty_when_marker_has_none():
    html = _marker_html([{"id": VIDEO_ID, "playUrls": ["https://v26-web.douyinvod.com/v/1"]}])
    assert _subtitle_urls_from_marker(FakePage(html), VIDEO_ID) == []


# ---------- 按标签选候选 ----------

def test_list_digest_candidates_filters_by_tag(tmp_path):
    db = DBManager(tmp_path / "t.sqlite3")
    ai = db.upsert_link(LinkRecord(url=VIDEO_URL, title="a", source="douyin_favorite"))
    db.update_tags(VIDEO_URL, "技术_AI/工具/开发")
    other = db.upsert_link(
        LinkRecord(url="https://www.douyin.com/video/7412345678901234569", title="b", source="douyin_favorite")
    )
    db.update_tags("https://www.douyin.com/video/7412345678901234569", "生活_健身")
    db.upsert_link(
        LinkRecord(url="https://www.douyin.com/video/7412345678901234570", title="c", source="douyin_favorite")
    )

    rows = db.list_digest_candidates(limit=10, tag="技术_AI/工具/开发")

    assert [int(r["id"]) for r in rows] == [ai]
    assert other not in [int(r["id"]) for r in rows]


def test_list_digest_candidates_multi_tag_is_or(tmp_path):
    db = DBManager(tmp_path / "t2.sqlite3")
    ai_url = "https://www.douyin.com/video/7412345678901234571"
    fit_url = "https://www.douyin.com/video/7412345678901234572"
    other_url = "https://www.douyin.com/video/7412345678901234573"
    for url, title in ((ai_url, "ai"), (fit_url, "fit"), (other_url, "other")):
        db.upsert_link(LinkRecord(url=url, title=title, source="douyin_favorite"))
    db.update_tags(ai_url, "技术_AI/工具/开发")
    db.update_tags(fit_url, "生活_健身")
    db.update_tags(other_url, "心理_职场")

    rows = db.list_digest_candidates(limit=10, tag="技术_AI/工具/开发,生活_健身")

    assert {r["url"] for r in rows} == {ai_url, fit_url}

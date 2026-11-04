"""视频简报 LLM 摘要与报告渲染的回归测试。

不触真实网络：requests.post 被打桩（仿 test_laterhub_tagger_fallback.py 的 FakeResponse 思路）。
覆盖主备降级口径、多模态请求体、文件名三形态、Markdown 结构子集与原子落盘。
"""

from __future__ import annotations

import json

import requests
import pytest

from apps.laterhub import video_digest_llm as mod
from apps.laterhub.tagger import LLMConfig
from apps.laterhub.video_digest_llm import (
    DigestContent,
    VideoDigestLLM,
    build_digest_filename,
    render_digest_markdown,
    split_text,
    write_digest_report,
)

PRIMARY = LLMConfig(base_url="https://primary.test", api_key="k1", model="glm-5.3-flash")
BACKUP = LLMConfig(base_url="https://backup.test", api_key="k2", model="deepseek-flash")


class FakeResponse(requests.Response):
    """真 requests.Response 子类：_format_error 用 isinstance 读状态码。"""

    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "") -> None:
        super().__init__()
        self.status_code = status_code
        self._content = json.dumps(payload).encode("utf-8") if payload is not None else text.encode("utf-8")


def _ok(content: str) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def _null_content() -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": None}}]})


def _http_error(status_code: int) -> requests.HTTPError:
    return requests.HTTPError(f"HTTP {status_code}", response=FakeResponse(status_code, text='{"error":"boom"}'))


def _digest_json(*, summary: str = "一句话总结", points: tuple[str, ...] = ("要点一", "要点二", "要点三"), note=None) -> str:
    return json.dumps({"summary": summary, "key_points": list(points), "note": note}, ensure_ascii=False)


def _patch_post(monkeypatch, per_model: dict):
    """按 payload['model'] 返回预设响应（Exception 则抛出），记录调用顺序与请求体。"""
    calls: list[str] = []
    payloads: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["model"])
        payloads.append(json)
        result = per_model[json["model"]]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(mod.requests, "post", fake_post)
    return calls, payloads


# ---------- generate_from_subtitle ----------


def test_subtitle_valid_json(monkeypatch):
    calls, payloads = _patch_post(monkeypatch, {PRIMARY.model: _ok(_digest_json(note="补充备注"))})

    result = VideoDigestLLM(PRIMARY, BACKUP).generate_from_subtitle(title="标题", owner="作者", subtitle_text="字幕正文")

    assert result == DigestContent(summary="一句话总结", key_points=["要点一", "要点二", "要点三"], note="补充备注")
    assert calls == [PRIMARY.model]
    payload = payloads[0]
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 3000
    assert isinstance(payload["messages"][1]["content"], str)


def test_primary_400_falls_through_to_backup(monkeypatch):
    calls, _ = _patch_post(monkeypatch, {PRIMARY.model: _http_error(400), BACKUP.model: _ok(_digest_json())})

    result = VideoDigestLLM(PRIMARY, BACKUP).generate_from_subtitle(title="标题", owner=None, subtitle_text="字幕")

    assert result.summary == "一句话总结"
    assert calls == [PRIMARY.model, BACKUP.model]


def test_both_400_raises_runtime_error_with_both(monkeypatch):
    _patch_post(monkeypatch, {PRIMARY.model: _http_error(400), BACKUP.model: _http_error(400)})

    with pytest.raises(RuntimeError) as excinfo:
        VideoDigestLLM(PRIMARY, BACKUP).generate_from_subtitle(title="标题", owner=None, subtitle_text="字幕")

    message = str(excinfo.value)
    assert "primary" in message and "backup" in message
    assert "400" in message


def test_null_content_falls_through_to_backup(monkeypatch):
    calls, _ = _patch_post(monkeypatch, {PRIMARY.model: _null_content(), BACKUP.model: _ok(_digest_json())})

    result = VideoDigestLLM(PRIMARY, BACKUP).generate_from_subtitle(title="标题", owner=None, subtitle_text="字幕")

    assert result.summary == "一句话总结"
    assert calls == [PRIMARY.model, BACKUP.model]


def test_key_points_too_few_falls_through_to_backup(monkeypatch):
    """key_points 只有 2 条 → ValueError → 走备用。"""
    calls, _ = _patch_post(
        monkeypatch,
        {PRIMARY.model: _ok(_digest_json(points=("只有一", "只有二"))), BACKUP.model: _ok(_digest_json())},
    )

    result = VideoDigestLLM(PRIMARY, BACKUP).generate_from_subtitle(title="标题", owner=None, subtitle_text="字幕")

    assert result.key_points == ["要点一", "要点二", "要点三"]
    assert calls == [PRIMARY.model, BACKUP.model]


def test_code_fenced_json_is_parsed(monkeypatch):
    fenced = "```json\n" + _digest_json() + "\n```"
    _patch_post(monkeypatch, {PRIMARY.model: _ok(fenced)})

    result = VideoDigestLLM(PRIMARY).generate_from_subtitle(title="标题", owner=None, subtitle_text="字幕")

    assert result.summary == "一句话总结"
    assert len(result.key_points) == 3


def test_key_points_over_seven_is_truncated(monkeypatch):
    many = tuple(f"要点{i}" for i in range(1, 10))
    _patch_post(monkeypatch, {PRIMARY.model: _ok(_digest_json(points=many))})

    result = VideoDigestLLM(PRIMARY).generate_from_subtitle(title="标题", owner=None, subtitle_text="字幕")

    assert len(result.key_points) == 7
    assert result.key_points == [f"要点{i}" for i in range(1, 8)]


# ---------- 长字幕分批 ----------


def test_split_text_covers_all_lines_with_overlap():
    text = "\n".join(f"第{i}行" for i in range(1, 31))
    chunks = split_text(text, max_chars=40, overlap_lines=1)

    assert len(chunks) > 1
    for i in range(1, 31):
        assert any(f"第{i}行" in chunk for chunk in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.splitlines()[0] == prev.splitlines()[-1]


def test_split_text_hard_splits_overlong_single_line():
    text = "字" * 25
    chunks = split_text(text, max_chars=10, overlap_lines=2)

    assert "".join(chunks) == text
    assert all(len(chunk) <= 10 for chunk in chunks)


def test_long_subtitle_is_chunked_then_merged(monkeypatch):
    payloads: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        payloads.append(json)
        if "response_format" in json:
            return _ok(_digest_json(summary="合并总结", note="合并备注"))
        return _ok("分段笔记")

    monkeypatch.setattr(mod.requests, "post", fake_post)
    text = "\n".join(["开头内容" * 20, "中间内容" * 3000, "结尾内容" * 20])

    result = VideoDigestLLM(PRIMARY).generate_from_subtitle(title="长视频", owner=None, subtitle_text=text)

    chunk_calls = [p for p in payloads if "response_format" not in p]
    merge_calls = [p for p in payloads if "response_format" in p]
    assert result == DigestContent(summary="合并总结", key_points=["要点一", "要点二", "要点三"], note="合并备注")
    assert len(chunk_calls) >= 2 and len(merge_calls) == 1

    # 全文覆盖：首尾都进入了分段请求，没有截断
    all_chunk_text = "\n".join(str(p["messages"][1]["content"]) for p in chunk_calls)
    assert "开头内容" * 20 in all_chunk_text
    assert "结尾内容" * 20 in all_chunk_text
    # 合并请求拿到全部分段笔记
    merge_user = str(merge_calls[0]["messages"][1]["content"])
    assert merge_user.count("分段笔记") == len(chunk_calls)


# ---------- generate_from_video_url ----------


def test_video_url_uses_multimodal_content_array(monkeypatch):
    calls, payloads = _patch_post(monkeypatch, {PRIMARY.model: _ok(_digest_json())})

    result = VideoDigestLLM(PRIMARY).generate_from_video_url(
        title="标题", owner=None, video_url="https://cdn.test/v.mp4?sign=abc"
    )

    content = payloads[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1] == {"type": "video_url", "video_url": {"url": "https://cdn.test/v.mp4?sign=abc"}}
    assert calls == [PRIMARY.model]
    assert result.summary == "一句话总结"


# ---------- build_digest_filename ----------


def test_build_digest_filename_variants():
    # 优先视频标题：清洗 Windows 非法字符，限长
    assert build_digest_filename("国家队偷偷加仓ETF，我写了个程序", "https://x") == "国家队偷偷加仓ETF，我写了个程序.md"
    assert build_digest_filename('a/b*c?d:e"f<g>h|i', "https://x") == "a b c d e f g h i.md"
    assert len(build_digest_filename("长" * 100, "https://x")) <= 60 + 3
    # 无标题兜底：平台 + id
    assert build_digest_filename("", "https://www.bilibili.com/video/BV1xx411c7mD/?spm=1") == (
        "video_digest_bilibili_BV1xx411c7mD.md"
    )
    assert build_digest_filename("  ", "https://www.douyin.com/video/7412345678901234567") == (
        "video_digest_douyin_7412345678901234567.md"
    )
    hashed = build_digest_filename("", "https://example.com/whatever?q=中文")
    assert hashed.startswith("video_digest_url_") and hashed.endswith(".md")


# ---------- render_digest_markdown ----------


def test_render_digest_markdown_structure():
    digest = DigestContent(summary="这是总结", key_points=["一", "二", "三"], note=None)
    md = render_digest_markdown(
        title="视频标题",
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        source="bilibili_watchlater",
        owner=None,
        duration_seconds=443,
        subtitle_source="bilibili_cc",
        generated_at="2026-09-29 10:00",
        digest=digest,
    )

    assert "# 视频标题" in md
    assert "链接：https://www.bilibili.com/video/BV1xx411c7mD" in md
    assert "来源：B站稍后看" in md
    assert "作者：未知" in md
    assert "时长：7分23秒" in md
    assert "字幕来源：B站字幕" in md
    assert "生成时间：2026-09-29 10:00" in md
    assert "## 一句话总结" in md and "这是总结" in md
    assert "## 核心要点" in md
    assert "- 一" in md and "- 二" in md and "- 三" in md
    assert "## 备注" in md and "无" in md
    # 仓库 md 渲染器子集：禁表格与代码围栏
    assert "|" not in md
    assert "```" not in md


def test_render_digest_markdown_note_priority_and_source_passthrough():
    with_note = render_digest_markdown(
        title="T",
        url="u",
        source="unknown_source",
        owner="某人",
        duration_seconds=None,
        subtitle_source="glm_video",
        generated_at="now",
        digest=DigestContent(summary="s", key_points=["a", "b", "c"], note="模型备注"),
    )
    assert "来源：unknown_source" in with_note
    assert "字幕来源：AI 视频理解" in with_note
    assert "时长：未知" in with_note
    assert "模型备注" in with_note

    no_note = render_digest_markdown(
        title="T",
        url="u",
        source="douyin_favorites",
        owner=None,
        duration_seconds=0,
        subtitle_source="videocaptioner_bijian",
        generated_at="now",
        digest=DigestContent(summary="s", key_points=["a", "b", "c"], note=None),
    )
    assert "无" in no_note


# ---------- write_digest_report ----------


def test_write_digest_report_atomic_utf8(tmp_path):
    report_dir = tmp_path / "reports"
    path = write_digest_report(report_dir, "video_digest_x.md", "# 中文标题\n内容")

    assert path == report_dir / "video_digest_x.md"
    assert path.read_text(encoding="utf-8") == "# 中文标题\n内容"
    assert "中文".encode("utf-8") in path.read_bytes()

    # 重复写覆盖，且不留 .tmp
    write_digest_report(report_dir, "video_digest_x.md", "第二版")
    assert path.read_text(encoding="utf-8") == "第二版"
    assert list(report_dir.glob("*.tmp")) == []

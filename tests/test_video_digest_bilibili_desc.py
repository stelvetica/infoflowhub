"""B站无人声时的分析依据：优先 B站简介，简介为空才回退 GLM 视频理解。

不触网络/浏览器：bsk 会话、connector、ASR 与 LLM 全部打桩。
"""

from __future__ import annotations

import connectors._shared.bsk_session as bsk_session_mod
from apps.laterhub import video_digest as vd
from apps.laterhub.db import DBManager, LinkRecord
from apps.laterhub.video_digest_llm import DigestContent
from connectors.bilibili import video as bili_video

BILI = "https://www.bilibili.com/video/BV1xx411c7mD"
DIGEST = DigestContent(summary="总结", key_points=["一", "二", "三"], note=None)


class FakeBskSession:
    def __init__(self, start_url=None) -> None:
        pass

    def start(self) -> None:
        pass

    def acquire_page(self):
        return object()

    def shutdown(self) -> None:
        pass


class FakeLLM:
    def __init__(self) -> None:
        self.subtitle_calls: list[dict] = []
        self.desc_calls: list[dict] = []
        self.video_calls: list[dict] = []

    def generate_from_subtitle(self, **kwargs) -> DigestContent:
        self.subtitle_calls.append(kwargs)
        return DIGEST

    def generate_from_description(self, **kwargs) -> DigestContent:
        self.desc_calls.append(kwargs)
        return DIGEST

    def generate_from_video_url(self, **kwargs) -> DigestContent:
        self.video_calls.append(kwargs)
        return DIGEST


def _setup(monkeypatch, tmp_path, *, description: str, cc: str | None, asr_error: bool = True):
    monkeypatch.setattr(bsk_session_mod, "BskRunnerSession", FakeBskSession)
    monkeypatch.setattr(vd, "TMP_DIR", tmp_path / "tmp")
    meta = bili_video.BilibiliVideoMeta(
        bvid="BV1xx411c7mD",
        aid=1,
        cid=2,
        title="视频标题",
        owner="UP",
        duration_seconds=31,
        description=description,
    )
    monkeypatch.setattr(bili_video, "get_bilibili_video_meta", lambda url, page: meta)
    monkeypatch.setattr(bili_video, "get_bilibili_cc_subtitle_text", lambda *a, **k: cc)

    def fake_download(*args, **kwargs):
        dest = kwargs["dest_dir"]
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "v.mp4"
        path.write_bytes(b"fake")
        return path

    monkeypatch.setattr(bili_video, "download_bilibili_video", fake_download)
    if asr_error:
        monkeypatch.setattr(
            vd,
            "_transcribe_with_videocaptioner",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("videocaptioner 未识别到有效语音")),
        )
    else:
        monkeypatch.setattr(vd, "_transcribe_with_videocaptioner", lambda *a, **k: None)

    db = DBManager(tmp_path / "d.sqlite3")
    link_id = db.upsert_link(LinkRecord(url=BILI, title="视频标题", source="bilibili_watchlater"))
    return db, link_id


def _report(reports_dir, db, link_id) -> str:
    path = reports_dir / db.get_digest_row(link_id)["digest_path"]
    return path.read_text(encoding="utf-8")


def test_no_speech_uses_bilibili_description_and_notes_work(tmp_path, monkeypatch):
    db, link_id = _setup(monkeypatch, tmp_path, description="简介：一款论文阅读器", cc=None)
    reports_dir = tmp_path / "reports"
    llm = FakeLLM()

    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"
    assert llm.desc_calls and llm.desc_calls[0]["description"] == "简介：一款论文阅读器"
    assert llm.subtitle_calls == [] and llm.video_calls == []
    markdown = _report(reports_dir, db, link_id)
    assert "字幕来源：B站简介" in markdown
    assert "处理说明：" in markdown
    assert "CC字幕未取到" in markdown
    assert "已下载视频" in markdown
    assert "未识别到有效人声" in markdown
    assert "改用B站简介分析" in markdown


def test_empty_description_falls_back_to_glm_video(tmp_path, monkeypatch):
    db, link_id = _setup(monkeypatch, tmp_path, description="", cc=None)
    monkeypatch.setattr(bili_video, "get_bilibili_full_mp4_url", lambda *a, **k: "https://cdn.test/v.mp4")
    reports_dir = tmp_path / "reports"
    llm = FakeLLM()

    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"
    assert llm.video_calls and llm.video_calls[0]["video_url"] == "https://cdn.test/v.mp4"
    assert llm.desc_calls == []
    markdown = _report(reports_dir, db, link_id)
    assert "字幕来源：AI 视频理解" in markdown
    assert "B站简介为空，改用AI视频理解" in markdown


def test_cc_success_also_passes_description(tmp_path, monkeypatch):
    cc = "这是一段足够长的字幕内容，用于验证简介也会参与所有B站视频的分析。"
    db, link_id = _setup(monkeypatch, tmp_path, description="简介：工具名称与链接", cc=cc)
    monkeypatch.setattr(
        bili_video,
        "download_bilibili_video",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("CC 成功后不应下载视频")),
    )
    reports_dir = tmp_path / "reports"
    llm = FakeLLM()

    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"
    assert llm.subtitle_calls[0]["description"] == "简介：工具名称与链接"
    assert llm.desc_calls == [] and llm.video_calls == []
    markdown = _report(reports_dir, db, link_id)
    assert "字幕来源：B站字幕" in markdown
    assert "处理说明：使用B站CC字幕；B站简介作为补充材料参与分析" in markdown

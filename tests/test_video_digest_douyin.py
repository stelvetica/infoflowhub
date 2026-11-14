"""抖音视频简报链路 + unsupported 存量重置的回归测试。

不触真实网络、不依赖 videocaptioner 与抖音 connector：
connectors.douyin.video 由并行 Worker 提供，本测试自洽——模块缺失时注入 stub 再导入编排，
随后对 download_douyin_video / _transcribe_with_videocaptioner / LLM 全部打桩。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

try:  # 真 connector 若已就绪则用之；否则注入 stub 保证导入成功
    from connectors.douyin.video import DouyinVideoMedia  # noqa: F401
except Exception:  # noqa: BLE001
    _stub = types.ModuleType("connectors.douyin.video")

    def _stub_download(*args, **kwargs):  # pragma: no cover - 占位，测试总会 monkeypatch
        raise RuntimeError("stub connector")

    _stub.download_douyin_video = _stub_download

    class DouyinVideoMedia:  # pragma: no cover - 仅在 connector 缺失时占位
        def __init__(self, **kw):
            self.__dict__.update(kw)

    sys.modules["connectors.douyin.video"] = _stub

from apps.laterhub import tagger, video_digest as vd
from apps.laterhub.db import DBManager, LinkRecord
from apps.laterhub.video_digest_llm import DigestContent

DOUYIN = "https://www.douyin.com/video/7412345678901234567"
BILI = "https://www.bilibili.com/video/BV1xx411c7mD"
BILI2 = "https://www.bilibili.com/video/BV1yy411c7mE"
ARTICLE = "https://mp.weixin.qq.com/s/abcdefg"


class FakeLLM:
    def __init__(self) -> None:
        self.digest = DigestContent(summary="总结", key_points=["一", "二", "三"], note=None)
        self.subtitle_calls: list[dict] = []
        self.video_calls: list[dict] = []
        self.desc_calls: list[dict] = []
        self.frame_calls: list[dict] = []

    def generate_from_subtitle(self, **kwargs) -> DigestContent:
        self.subtitle_calls.append(kwargs)
        return self.digest

    def generate_from_description(self, **kwargs) -> DigestContent:
        self.desc_calls.append(kwargs)
        return self.digest

    def generate_from_frames(self, **kwargs) -> DigestContent:
        self.frame_calls.append(kwargs)
        return self.digest

    def generate_from_video_url(self, **kwargs) -> DigestContent:
        self.video_calls.append(kwargs)
        return self.digest


def _add(db: DBManager, url: str, *, source: str = "douyin_favorites", title: str = "视频") -> int:
    return db.upsert_link(LinkRecord(url=url, title=title, source=source))


def _patch_tmp(monkeypatch, tmp_path: Path) -> Path:
    tmp = tmp_path / "tmp"
    monkeypatch.setattr(vd, "TMP_DIR", tmp)
    return tmp


def _fake_download(duration: int = 65, subtitles: list[str] | None = None):
    def fake(url, *, dest_dir, session=None, max_bytes=2**31, timeout=1800.0, resolved=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        video = dest_dir / "video.mp4"
        video.write_bytes(b"fake-mp4")
        if resolved is not None:
            resolved["media"] = DouyinVideoMedia(
                video_url="https://v3-web.douyinvod.com/x/",
                audio_url=None,
                subtitle_urls=list(subtitles or []),
            )
        return video, duration

    return fake


def _fake_transcribe(text: str = "抖音字幕正文"):
    def fake(video_path, workdir, **kwargs):
        srt = workdir / "asr.srt"
        srt.write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n", encoding="utf-8")
        return srt

    return fake


# ---------- process_one：抖音链路 ----------


def test_douyin_full_chain_succeeds_and_cleans_workdir(tmp_path, monkeypatch):
    db = DBManager(tmp_path / "d.sqlite3")
    link_id = _add(db, DOUYIN)
    tmp = _patch_tmp(monkeypatch, tmp_path)
    reports_dir = tmp_path / "reports"
    workdir = tmp / str(link_id)

    monkeypatch.setattr(vd, "download_douyin_video", _fake_download(duration=65))
    monkeypatch.setattr(vd, "_transcribe_media", _fake_transcribe("抖音字幕正文"))

    llm = FakeLLM()
    assert db.claim_digest(link_id) is True
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"
    row = db.get_digest_row(link_id)
    assert row["digest_status"] == "succeeded"
    assert row["digest_attempts"] == 1
    assert row["digest_error"] is None
    assert row["digest_path"].startswith("video_digest/")  # 子目录/标题.md
    assert (reports_dir / row["digest_path"]).is_file()

    call = llm.subtitle_calls[0]
    assert call["owner"] is None
    assert "抖音字幕正文" in call["subtitle_text"]
    assert llm.video_calls == []  # 抖音不走 GLM video_url 兜底
    # 报告末尾必须给出原文，读者不用回看视频
    report = (reports_dir / row["digest_path"]).read_text(encoding="utf-8")
    assert "## 原文" in report
    assert report.rstrip().endswith("抖音字幕正文")
    assert not workdir.exists()  # finally 清理


def test_douyin_download_runtime_error_marks_failed(tmp_path, monkeypatch):
    db = DBManager(tmp_path / "d2.sqlite3")
    link_id = _add(db, DOUYIN)
    _patch_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(vd, "download_douyin_video", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("直链已过期")))

    llm = FakeLLM()
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=tmp_path / "reports")

    assert outcome.status == "failed"
    row = db.get_digest_row(link_id)
    assert row["digest_status"] == "failed"
    assert "下载" in row["digest_error"]
    assert "直链已过期" in row["digest_error"]
    assert llm.subtitle_calls == []


def test_douyin_transcribe_error_marks_failed(tmp_path, monkeypatch):
    db = DBManager(tmp_path / "d3.sqlite3")
    link_id = _add(db, DOUYIN)
    _patch_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(vd, "download_douyin_video", _fake_download())
    monkeypatch.setattr(
        vd, "_transcribe_media", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bijian boom"))
    )

    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=FakeLLM(), reports_dir=tmp_path / "reports")

    assert outcome.status == "failed"
    row = db.get_digest_row(link_id)
    assert row["digest_status"] == "failed"
    assert "转写" in row["digest_error"]


# ---------- 无人声：退回作品文案 ----------


def test_douyin_no_speech_falls_back_to_caption(tmp_path, monkeypatch):
    db = DBManager(tmp_path / "d4.sqlite3")
    title = "uo：孩子ne老不好，多半是废了 #卡黄 #已屏蔽正主 #snh48"
    link_id = _add(db, DOUYIN, title=title)
    _patch_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(vd, "download_douyin_video", _fake_download(duration=13))
    monkeypatch.setattr(
        vd,
        "_transcribe_media",
        lambda *a, **k: (_ for _ in ()).throw(vd.NoSpeechDetected("结果过短(0字)")),
    )

    llm = FakeLLM()
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=tmp_path / "reports")

    assert outcome.status == "succeeded"
    assert llm.subtitle_calls == []
    call = llm.desc_calls[0]
    assert call["owner"] == "uo"
    assert call["description"].startswith("孩子ne老不好")
    row = db.get_digest_row(link_id)
    assert row["digest_status"] == "succeeded"
    report = (tmp_path / "reports" / row["digest_path"]).read_text(encoding="utf-8")
    assert "抖音文案" in report
    assert "未识别到人声" in report
    # 无人声时原文就是作品文案
    assert "## 原文" in report and "孩子ne老不好" in report


def test_douyin_no_speech_without_author_prefix_keeps_title_as_caption(tmp_path, monkeypatch):
    db = DBManager(tmp_path / "d5.sqlite3")
    link_id = _add(db, DOUYIN, title="纯音乐剪辑")
    _patch_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(vd, "download_douyin_video", _fake_download(duration=10))
    monkeypatch.setattr(
        vd,
        "_transcribe_media",
        lambda *a, **k: (_ for _ in ()).throw(vd.NoSpeechDetected("结果过短(0字)")),
    )

    llm = FakeLLM()
    vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=tmp_path / "reports")

    call = llm.desc_calls[0]
    assert call["owner"] is None
    assert call["description"] == "纯音乐剪辑"


def _fake_frames(tmp_path: Path, count: int = 2) -> list[Path]:
    frames = []
    for index in range(count):
        frame = tmp_path / f"frame_{index}.jpg"
        frame.write_bytes(b"\xff\xd8\xff\xd9")
        frames.append(frame)
    return frames


def _douyin_no_speech_case(tmp_path, monkeypatch, db_name: str, title: str = "芊：某教程 #tag"):
    db = DBManager(tmp_path / db_name)
    link_id = _add(db, DOUYIN, title=title)
    _patch_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(vd, "download_douyin_video", _fake_download(duration=13))
    monkeypatch.setattr(
        vd,
        "_transcribe_media",
        lambda *a, **k: (_ for _ in ()).throw(vd.NoSpeechDetected("结果过短(0字)")),
    )
    return db, link_id, tmp_path / "reports"


def test_douyin_no_speech_prefers_frames(tmp_path, monkeypatch):
    db, link_id, reports_dir = _douyin_no_speech_case(tmp_path, monkeypatch, "d6.sqlite3")
    monkeypatch.setattr(vd, "_extract_frames", lambda *a, **k: _fake_frames(tmp_path))

    llm = FakeLLM()
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"
    assert llm.desc_calls == []  # 看图成功就不必退回文案
    call = llm.frame_calls[0]
    assert call["owner"] == "芊"
    assert len(call["frames"]) == 2
    assert call["description"].startswith("某教程")
    report = (reports_dir / db.get_digest_row(link_id)["digest_path"]).read_text(encoding="utf-8")
    assert "视频截图理解" in report
    assert "截图做画面理解" in report


def test_douyin_frames_failure_falls_back_to_caption(tmp_path, monkeypatch):
    db, link_id, reports_dir = _douyin_no_speech_case(tmp_path, monkeypatch, "d7.sqlite3")
    monkeypatch.setattr(vd, "_extract_frames", lambda *a, **k: _fake_frames(tmp_path))

    llm = FakeLLM()
    monkeypatch.setattr(
        llm, "generate_from_frames", lambda **k: (_ for _ in ()).throw(RuntimeError("看图挂了"))
    )
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"  # 看图失败不连坐整条
    assert llm.desc_calls
    report = (reports_dir / db.get_digest_row(link_id)["digest_path"]).read_text(encoding="utf-8")
    assert "抖音文案" in report


def test_douyin_no_frames_falls_back_to_caption(tmp_path, monkeypatch):
    db, link_id, reports_dir = _douyin_no_speech_case(tmp_path, monkeypatch, "d8.sqlite3")
    monkeypatch.setattr(vd, "_extract_frames", lambda *a, **k: [])

    llm = FakeLLM()
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"
    assert llm.frame_calls == []
    assert llm.desc_calls


def test_extract_frames_without_ffmpeg_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(vd.shutil, "which", lambda name: None)
    assert vd._extract_frames(tmp_path / "v.mp4", 13, tmp_path) == []


def test_extract_frames_uses_midpoints_and_skips_empty_output(monkeypatch, tmp_path):
    monkeypatch.setattr(vd.shutil, "which", lambda name: "ffmpeg")
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[cmd.index("-ss") + 1])
        Path(cmd[-1]).write_bytes(b"jpeg")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(vd.subprocess, "run", fake_run)
    frames = vd._extract_frames(tmp_path / "v.mp4", 11, tmp_path, count=4)

    assert len(frames) == 4
    assert len(seen) == 4
    timestamps = [float(value) for value in seen]
    assert timestamps == sorted(timestamps)
    assert all(0 < value < 11 for value in timestamps)


def test_split_douyin_caption_guards():
    assert vd._split_douyin_caption("uo：文案 #tag") == ("uo", "文案 #tag")
    # 冒号前过长/为空都不拆
    long_prefix = "x" * 30
    assert vd._split_douyin_caption(f"{long_prefix}：文案") == (None, f"{long_prefix}：文案")
    assert vd._split_douyin_caption("：文案") == (None, "：文案")
    assert vd._split_douyin_caption("无冒号文案") == (None, "无冒号文案")


def test_transcribe_raises_no_speech_only_when_engines_actually_ran(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr(vd.shutil, "which", lambda name: "videocaptioner")

    def empty_run(cmd, **kwargs):
        # 两个引擎都 exit=0，但产出的 SRT 没有正文（纯音乐/无人声）
        Path(cmd[cmd.index("-o") + 1]).write_text("1\n00:00:00,000 --> 00:00:01,000\n\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vd.subprocess, "run", empty_run)
    with pytest.raises(vd.NoSpeechDetected):
        vd._transcribe_with_videocaptioner(tmp_path / "v.mp4", tmp_path, duration_seconds=13)

    def broken_run(cmd, **kwargs):
        # 引擎自己报错：工具链坏了，属于真失败，不能拿文案顶替
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(vd.subprocess, "run", broken_run)
    with pytest.raises(RuntimeError) as excinfo:
        vd._transcribe_with_videocaptioner(tmp_path / "v.mp4", tmp_path, duration_seconds=13)
    assert not isinstance(excinfo.value, vd.NoSpeechDetected)


def test_douyin_long_subtitle_is_passed_whole_to_llm(tmp_path, monkeypatch):
    """编排层不再截断：长字幕原样交给 LLM 层，由其分块处理。"""
    db = DBManager(tmp_path / "d4.sqlite3")
    link_id = _add(db, DOUYIN)
    _patch_tmp(monkeypatch, tmp_path)
    long_text = "字" * 30000
    monkeypatch.setattr(vd, "download_douyin_video", _fake_download())
    monkeypatch.setattr(vd, "_transcribe_media", _fake_transcribe(long_text))

    llm = FakeLLM()
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=tmp_path / "reports")

    assert outcome.status == "succeeded"
    assert llm.subtitle_calls[0]["subtitle_text"] == long_text


# ---------- db.reset_unsupported_to_pending ----------


def test_reset_unsupported_only_touches_video_rows(tmp_path):
    db = DBManager(tmp_path / "r.sqlite3")
    u_douyin = _add(db, DOUYIN)
    u_bili = _add(db, BILI)
    u_article = _add(db, ARTICLE, source="wechat")
    pending = _add(db, BILI2)
    failed = _add(db, "https://www.bilibili.com/video/BV1zz411c7mF")

    db.mark_digest_unsupported(u_douyin, error_message="抖音暂未支持")
    db.mark_digest_unsupported(u_bili, error_message="不支持")
    db.mark_digest_unsupported(u_article, error_message="非视频")
    db.mark_digest_failed(failed, error_message="boom")

    assert db.reset_unsupported_to_pending() == 2  # 只重置视频条目

    assert db.get_digest_row(u_douyin)["digest_status"] == "pending"
    assert db.get_digest_row(u_douyin)["digest_error"] is None
    assert db.get_digest_row(u_bili)["digest_status"] == "pending"
    assert db.get_digest_row(u_bili)["digest_error"] is None
    assert db.get_digest_row(u_article)["digest_status"] == "unsupported"
    assert db.get_digest_row(pending)["digest_status"] == "pending"
    assert db.get_digest_row(failed)["digest_status"] == "failed"
    assert db.reset_unsupported_to_pending() == 0  # 幂等


# ---------- run_video_digest / parse_args ----------


def test_run_video_digest_retry_unsupported_resets_and_retries(tmp_path, monkeypatch):
    db_path = tmp_path / "run.sqlite3"
    db = DBManager(db_path)
    link_id = _add(db, DOUYIN)
    db.mark_digest_unsupported(link_id, error_message="抖音暂未支持")

    _patch_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(tagger, "load_project_env", lambda *a, **k: None)
    monkeypatch.setattr(tagger.LLMConfig, "from_env", classmethod(lambda cls, *a, **k: object()))
    monkeypatch.setattr(vd, "VideoDigestLLM", lambda *a, **k: FakeLLM())
    monkeypatch.setattr(vd, "download_douyin_video", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("直链已过期")))

    summary = vd.run_video_digest(db_path=db_path, limit=5, retry_unsupported=True)

    assert summary.reset_count == 1
    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].link_id == link_id
    assert summary.outcomes[0].status == "failed"
    # 已从 unsupported 解锁并被候选查询选中重试（列表路径不 claim，故不校验 attempts）
    assert db.get_digest_row(link_id)["digest_status"] == "failed"


def test_run_video_digest_without_flag_leaves_unsupported(tmp_path, monkeypatch):
    db_path = tmp_path / "run2.sqlite3"
    db = DBManager(db_path)
    link_id = _add(db, DOUYIN)
    db.mark_digest_unsupported(link_id, error_message="抖音暂未支持")

    _patch_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(tagger, "load_project_env", lambda *a, **k: None)
    monkeypatch.setattr(tagger.LLMConfig, "from_env", classmethod(lambda cls, *a, **k: object()))
    monkeypatch.setattr(vd, "VideoDigestLLM", lambda *a, **k: FakeLLM())

    summary = vd.run_video_digest(db_path=db_path, limit=5)

    assert summary.reset_count == 0
    assert summary.outcomes == []
    assert db.get_digest_row(link_id)["digest_status"] == "unsupported"


def test_parse_args_retry_unsupported():
    assert vd.parse_args([]).retry_unsupported is False
    assert vd.parse_args(["--retry-unsupported"]).retry_unsupported is True
    assert vd.parse_args(["--retry-unsupported", "--retry-failed"]).retry_failed is True


# ---------- 自带字幕优先 ----------


def test_douyin_subtitle_first_skips_asr(tmp_path, monkeypatch):
    db = DBManager(tmp_path / "s.sqlite3")
    link_id = _add(db, DOUYIN)
    _patch_tmp(monkeypatch, tmp_path)
    reports_dir = tmp_path / "reports"

    subtitle_url = "https://v26-web.douyinvod.com/sub/abc.srt"
    monkeypatch.setattr(vd, "download_douyin_video", _fake_download(duration=60, subtitles=[subtitle_url]))
    monkeypatch.setattr(
        vd,
        "fetch_douyin_subtitle_text",
        lambda url, **kw: (
            "1\n00:00:00,000 --> 00:00:02,000\n自带字幕第一句，讲的是检索增强的完整链路\n"
            "2\n00:00:02,000 --> 00:00:04,000\n自带字幕第二句，讲的是分块与召回评估\n"
        ),
    )

    called = []
    monkeypatch.setattr(vd, "_transcribe_media", lambda *a, **k: called.append(1))

    llm = FakeLLM()
    assert db.claim_digest(link_id) is True
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"
    assert called == []  # 有字幕就不该再跑语音转写
    assert llm.subtitle_calls and "自带字幕第一句" in llm.subtitle_calls[0]["subtitle_text"]
    report = next((reports_dir / vd.DIGEST_SUBDIR).glob("*.md")).read_text(encoding="utf-8")
    assert "自带字幕" in report


def test_douyin_short_subtitle_falls_back_to_asr(tmp_path, monkeypatch):
    db = DBManager(tmp_path / "s2.sqlite3")
    link_id = _add(db, DOUYIN)
    _patch_tmp(monkeypatch, tmp_path)
    reports_dir = tmp_path / "reports"

    monkeypatch.setattr(
        vd, "download_douyin_video",
        _fake_download(duration=120, subtitles=["https://v26-web.douyinvod.com/sub/short.srt"]),
    )
    monkeypatch.setattr(vd, "fetch_douyin_subtitle_text", lambda url, **kw: "只有一句")
    monkeypatch.setattr(vd, "_transcribe_media", _fake_transcribe("转写正文"))

    llm = FakeLLM()
    assert db.claim_digest(link_id) is True
    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=llm, reports_dir=reports_dir)

    assert outcome.status == "succeeded"
    assert llm.subtitle_calls[0]["subtitle_text"].strip() == "转写正文"


def test_subtitle_to_text_supports_json_srt_vtt():
    assert vd._subtitle_to_text('{"utterances":[{"text":"甲"},{"content":"乙"}]}') == "甲\n乙"
    assert vd._subtitle_to_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n字幕行") == "字幕行"
    assert vd._subtitle_to_text("1\n00:00:01,000 --> 00:00:02,000\n<b>加粗行</b>") == "加粗行"


# ---------- 抖音批次节奏 ----------


def test_douyin_batch_gap_escalates_every_ten_and_caps(monkeypatch):
    monkeypatch.setattr(vd, "DOUYIN_BATCH_GAP_JITTER_SECONDS", 0)
    assert vd._douyin_batch_gap_seconds(0) == 10
    assert vd._douyin_batch_gap_seconds(9) == 10
    assert vd._douyin_batch_gap_seconds(10) == 18
    assert vd._douyin_batch_gap_seconds(30) == 34
    assert vd._douyin_batch_gap_seconds(10_000) == 40  # 撞上限，不会无限变慢


def test_douyin_batch_gap_has_random_jitter(monkeypatch):
    monkeypatch.setattr(vd, "DOUYIN_BATCH_GAP_JITTER_SECONDS", 12)
    gaps = {vd._douyin_batch_gap_seconds(0) for _ in range(50)}
    assert len(gaps) > 1  # 每次都带随机量
    assert all(10 <= gap <= 22 for gap in gaps)


def test_run_video_digest_breaks_after_consecutive_failures(tmp_path, monkeypatch):
    db_path = tmp_path / "run3.sqlite3"
    db = DBManager(db_path)
    for i in range(6):
        _add(db, f"https://www.douyin.com/video/7412345678901234{i:02d}")

    _patch_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(tagger, "load_project_env", lambda *a, **k: None)
    monkeypatch.setattr(tagger.LLMConfig, "from_env", classmethod(lambda cls, *a, **k: object()))
    monkeypatch.setattr(vd, "VideoDigestLLM", lambda *a, **k: FakeLLM())
    monkeypatch.setattr(vd, "_douyin_batch_gap_seconds", lambda done: 0)
    monkeypatch.setattr(
        vd,
        "process_one",
        lambda db, row, *, llm, keep_temp=False: vd.DigestOutcome(
            int(row["id"]), str(row["url"]), str(row["title"]), "failed", "boom"
        ),
    )

    summary = vd.run_video_digest(db_path=db_path, limit=6)

    assert len(summary.outcomes) == 3  # 第 3 条失败后立即熔断，不再消耗剩余条目
    assert "连续 3 条抖音失败" in summary.aborted

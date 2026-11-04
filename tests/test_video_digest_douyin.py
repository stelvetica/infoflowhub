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
    import connectors.douyin.video  # noqa: F401
except Exception:  # noqa: BLE001
    _stub = types.ModuleType("connectors.douyin.video")

    def _stub_download(*args, **kwargs):  # pragma: no cover - 占位，测试总会 monkeypatch
        raise RuntimeError("stub connector")

    _stub.download_douyin_video = _stub_download
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

    def generate_from_subtitle(self, **kwargs) -> DigestContent:
        self.subtitle_calls.append(kwargs)
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


def _fake_download(duration: int = 65):
    def fake(url, *, dest_dir, session=None, max_bytes=2**31, timeout=1800.0):
        dest_dir.mkdir(parents=True, exist_ok=True)
        video = dest_dir / "video.mp4"
        video.write_bytes(b"fake-mp4")
        return video, duration

    return fake


def _fake_transcribe(text: str = "抖音字幕正文"):
    def fake(video_path, workdir):
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
    monkeypatch.setattr(vd, "_transcribe_with_videocaptioner", _fake_transcribe("抖音字幕正文"))

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
        vd, "_transcribe_with_videocaptioner", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bijian boom"))
    )

    outcome = vd.process_one(db, db.get_digest_row(link_id), llm=FakeLLM(), reports_dir=tmp_path / "reports")

    assert outcome.status == "failed"
    row = db.get_digest_row(link_id)
    assert row["digest_status"] == "failed"
    assert "转写" in row["digest_error"]


def test_douyin_long_subtitle_is_passed_whole_to_llm(tmp_path, monkeypatch):
    """编排层不再截断：长字幕原样交给 LLM 层，由其分块处理。"""
    db = DBManager(tmp_path / "d4.sqlite3")
    link_id = _add(db, DOUYIN)
    _patch_tmp(monkeypatch, tmp_path)
    long_text = "字" * 30000
    monkeypatch.setattr(vd, "download_douyin_video", _fake_download())
    monkeypatch.setattr(vd, "_transcribe_with_videocaptioner", _fake_transcribe(long_text))

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

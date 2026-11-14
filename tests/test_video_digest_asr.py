"""ASR 引擎互备：空/过短结果不能被当成成功（bijian 会以 exit=0 返回 0 字节 SRT）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apps.laterhub import video_digest as vd


def _srt(text: str) -> str:
    return f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n"


def _patch_engines(monkeypatch: pytest.MonkeyPatch, texts: dict[str, str]):
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        asr = cmd[cmd.index("--asr") + 1]
        out = Path(cmd[cmd.index("-o") + 1])
        calls.append(asr)
        out.write_text(_srt(texts[asr]), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(vd.shutil, "which", lambda name: "videocaptioner")
    monkeypatch.setattr(vd.subprocess, "run", fake_run)
    return calls


def test_min_chars_is_conservative():
    assert vd._asr_min_chars(None) == 3
    assert vd._asr_min_chars(10) == 3
    assert vd._asr_min_chars(31) == 9
    assert vd._asr_min_chars(600) == 180


def test_empty_first_engine_falls_through_to_next(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    calls = _patch_engines(monkeypatch, {"bijian": "", "jianying": "这是一段真实的语音内容"})

    srt = vd._transcribe_with_videocaptioner(video, tmp_path, duration_seconds=10)

    assert calls == ["bijian", "jianying"]
    assert "真实的语音内容" in vd._srt_to_text(srt)


def test_bgm_only_result_counts_as_failure(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")
    calls = _patch_engines(monkeypatch, {"bijian": "", "jianying": "my love"})

    with pytest.raises(RuntimeError) as excinfo:
        vd._transcribe_with_videocaptioner(video, tmp_path, duration_seconds=31)

    assert calls == ["bijian", "jianying"]
    message = str(excinfo.value)
    assert "过短" in message and "bijian" in message and "jianying" in message

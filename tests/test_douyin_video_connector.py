"""抖音视频 connector 的离线回归。

零真实网络 / 零真实 bsk：bsk 会话、requests 会话、ffmpeg/ffprobe 全部打桩。
覆盖：URL 判定/ID 提取、直链解析（含假执行防护）、下载合流（双流/单流）、错误清理。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from connectors.douyin import video as video_mod

VIDEO_ID = "7300000000000000000"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"
V_STREAM = "https://v3-web.douyinvod.com/abc/media-video-1.mp4?sig=1"
A_STREAM = "https://v3-web.douyinvod.com/abc/media-audio-1.mp4?sig=2"


# --- 打桩件 -------------------------------------------------------------------

class FakeResponse:
    def __init__(self, *, status_code: int = 200, content: bytes = b"") -> None:
        self.status_code = status_code
        self._content = content

    def iter_content(self, chunk_size: int = 1):
        yield self._content

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeRequestsSession:
    """按 URL 路由到预设 FakeResponse 的最小 requests 会话。"""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []

    def get(self, url, *, timeout=None, stream=False, headers=None, params=None):
        self.calls.append({"url": url, "stream": stream})
        return self.handler(url)


class FakePage:
    def __init__(self, value) -> None:
        self._value = value
        self.goto_calls: list[str] = []
        self.waited: list[int] = []

    def goto(self, url, *, wait_until=None, timeout=None):
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms: int) -> None:
        self.waited.append(ms)

    def evaluate(self, js):
        return self._value


class FakeBskSession:
    """记录 start/shutdown 的 bsk 会话替身。"""

    def __init__(self, page) -> None:
        self._page = page
        self.started = 0
        self.shutdowns = 0

    def start(self) -> None:
        self.started += 1

    def shutdown(self) -> None:
        self.shutdowns += 1

    def acquire_page(self):
        return self._page


def _media(**kw) -> video_mod.DouyinVideoMedia:
    return video_mod.DouyinVideoMedia(**kw)


# --- 1) URL 判定 --------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/video/7300000000000000000",
        "https://www.douyin.com/video/7300000000000000000?previous_page=web_code_link",
        "https://douyin.com/video/123",
    ],
)
def test_is_douyin_video_url_positive(url):
    assert video_mod.is_douyin_video_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bilibili.com/video/BV1Q95N6rEXY",
        "https://www.douyin.com/video/abc",
        "https://www.douyin.com/user/self",
        "",
    ],
)
def test_is_douyin_video_url_negative(url):
    assert video_mod.is_douyin_video_url(url) is False


# --- 2) ID 提取 ---------------------------------------------------------------

def test_extract_id_standard():
    assert video_mod.extract_douyin_video_id(VIDEO_URL) == VIDEO_ID


def test_extract_id_with_query():
    assert video_mod.extract_douyin_video_id(f"{VIDEO_URL}?x=1") == VIDEO_ID


def test_extract_id_invalid_returns_empty():
    assert video_mod.extract_douyin_video_id("https://www.douyin.com/video/abc") == ""
    assert video_mod.extract_douyin_video_id("") == ""


# --- 3) 直链解析 --------------------------------------------------------------

def test_resolve_returns_media(monkeypatch):
    page = FakePage({"media": [V_STREAM, A_STREAM], "all_count": 2})
    session = FakeBskSession(page)

    media = video_mod.resolve_douyin_video_media(VIDEO_URL, session=session)

    assert media.video_url == V_STREAM
    assert media.audio_url == A_STREAM
    # 传入会话由调用方管理：不 start、不 shutdown
    assert session.started == 0
    assert session.shutdowns == 0
    assert page.goto_calls == [VIDEO_URL]


def test_resolve_video_only_audio_none(monkeypatch):
    page = FakePage({"media": [V_STREAM], "all_count": 1})
    media = video_mod.resolve_douyin_video_media(VIDEO_URL, session=FakeBskSession(page))
    assert media.video_url == V_STREAM
    assert media.audio_url is None


def test_resolve_non_dict_raises():
    # bsk 假执行会回函数自身/字符串，不能静默当「无直链」
    page = FakePage("function () {}")
    with pytest.raises(RuntimeError) as excinfo:
        video_mod.resolve_douyin_video_media(VIDEO_URL, session=FakeBskSession(page))
    assert "返回异常类型" in str(excinfo.value)


def test_resolve_audio_only_falls_back(monkeypatch):
    # 播放器可能只发音频流：ASR 只需音轨，video_url 回退为音频流
    page = FakePage({"media": [A_STREAM], "all_count": 1})
    media = video_mod.resolve_douyin_video_media(VIDEO_URL, session=FakeBskSession(page))
    assert media.video_url == A_STREAM
    assert media.audio_url == A_STREAM


def test_resolve_no_media_raises():
    page = FakePage({"media": [], "all_count": 0})
    with pytest.raises(RuntimeError) as excinfo:
        video_mod.resolve_douyin_video_media(VIDEO_URL, session=FakeBskSession(page))
    assert "未从抖音视频页提取到可下载直链" in str(excinfo.value)


def test_resolve_own_session_shuts_down(monkeypatch):
    page = FakePage({"media": [V_STREAM], "all_count": 1})
    session = FakeBskSession(page)
    monkeypatch.setattr(video_mod, "BskRunnerSession", lambda **kw: session)

    video_mod.resolve_douyin_video_media(VIDEO_URL)

    assert session.started == 1
    assert session.shutdowns == 1


# --- 4) 下载 ------------------------------------------------------------------

def _patch_media(monkeypatch, media):
    monkeypatch.setattr(video_mod, "resolve_douyin_video_media", lambda *a, **kw: media)


def _patch_requests(monkeypatch, handler):
    session = FakeRequestsSession(handler)
    monkeypatch.setattr(video_mod, "_build_download_session", lambda: session)
    return session


def _route_streams():
    def handler(url):
        if "media-audio-" in url:
            return FakeResponse(content=b"AUDIO")
        return FakeResponse(content=b"VIDEO")

    return handler


def _fake_tools(monkeypatch):
    """打桩 ffmpeg/ffprobe：which 返回假路径，run 产出成片 / 返回时长。"""
    monkeypatch.setattr(video_mod.shutil, "which", lambda name: f"C:/fake/{name}.exe")
    run_calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True):
        run_calls.append(list(cmd))
        if cmd[0].endswith("ffprobe.exe"):
            return SimpleNamespace(returncode=0, stdout=json.dumps({"format": {"duration": "90.0"}}), stderr="")
        Path(cmd[-1]).write_bytes(b"MERGED")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video_mod.subprocess, "run", fake_run)
    return run_calls


def test_download_two_streams_merges(monkeypatch, tmp_path):
    _patch_media(monkeypatch, _media(video_url=V_STREAM, audio_url=A_STREAM))
    _patch_requests(monkeypatch, _route_streams())
    run_calls = _fake_tools(monkeypatch)

    path, duration = video_mod.download_douyin_video(VIDEO_URL, dest_dir=tmp_path)

    assert path == tmp_path / f"{VIDEO_ID}.mp4"
    assert path.read_bytes() == b"MERGED"
    assert duration == 90
    ffmpeg_cmd = next(c for c in run_calls if c[0].endswith("ffmpeg.exe"))
    assert ffmpeg_cmd.count("-i") == 2
    assert "-c" in ffmpeg_cmd and "copy" in ffmpeg_cmd
    # 中间流已清理
    assert not (tmp_path / f"{VIDEO_ID}.video.mp4").exists()
    assert not (tmp_path / f"{VIDEO_ID}.audio.mp4").exists()


def test_download_single_stream_converts(monkeypatch, tmp_path):
    _patch_media(monkeypatch, _media(video_url=V_STREAM, audio_url=None))
    _patch_requests(monkeypatch, _route_streams())
    run_calls = _fake_tools(monkeypatch)

    path, duration = video_mod.download_douyin_video(VIDEO_URL, dest_dir=tmp_path)

    assert path.read_bytes() == b"MERGED"
    assert duration == 90
    ffmpeg_cmd = next(c for c in run_calls if c[0].endswith("ffmpeg.exe"))
    assert ffmpeg_cmd.count("-i") == 1
    assert "-c" in ffmpeg_cmd and "copy" in ffmpeg_cmd


def test_download_http_500_raises_and_cleans(monkeypatch, tmp_path):
    _patch_media(monkeypatch, _media(video_url=V_STREAM, audio_url=A_STREAM))
    _patch_requests(monkeypatch, lambda url: FakeResponse(status_code=500))
    _fake_tools(monkeypatch)

    with pytest.raises(RuntimeError) as excinfo:
        video_mod.download_douyin_video(VIDEO_URL, dest_dir=tmp_path)

    assert "HTTP 500" in str(excinfo.value)
    assert not (tmp_path / f"{VIDEO_ID}.video.mp4").exists()
    assert not (tmp_path / f"{VIDEO_ID}.mp4").exists()


def test_download_ffmpeg_missing_raises(monkeypatch, tmp_path):
    _patch_media(monkeypatch, _media(video_url=V_STREAM, audio_url=None))
    _patch_requests(monkeypatch, _route_streams())
    monkeypatch.setattr(video_mod.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError) as excinfo:
        video_mod.download_douyin_video(VIDEO_URL, dest_dir=tmp_path)

    assert "未找到 ffmpeg" in str(excinfo.value)
    assert not (tmp_path / f"{VIDEO_ID}.video.mp4").exists()


def test_download_ffprobe_failure_returns_none_duration(monkeypatch, tmp_path):
    _patch_media(monkeypatch, _media(video_url=V_STREAM, audio_url=None))
    _patch_requests(monkeypatch, _route_streams())
    monkeypatch.setattr(video_mod.shutil, "which", lambda name: f"C:/fake/{name}.exe")

    def fake_run(cmd, capture_output=True, text=True):
        if cmd[0].endswith("ffprobe.exe"):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        Path(cmd[-1]).write_bytes(b"MERGED")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video_mod.subprocess, "run", fake_run)

    path, duration = video_mod.download_douyin_video(VIDEO_URL, dest_dir=tmp_path)

    assert path.exists()
    assert duration is None

"""B站视频 connector 的离线回归。

不触真实网络：`_build_session` 被打桩成 FakeSession，请求按 URL 路由到预设载荷。
覆盖：URL 判定/ID 提取、CC 字幕解析、完整 mp4 直链与试看截断、dash/durl 下载、接口错误。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from connectors.bilibili import video as video_mod

BVID = "BV1Q95N6rEXY"


class FakeResponse:
    def __init__(self, payload: dict | None = None, *, status_code: int = 200, content: bytes = b"") -> None:
        self._payload = payload
        self.status_code = status_code
        self._content = content

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1):
        yield self._content

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeSession:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []

    def get(self, url, *, params=None, timeout=None, headers=None, stream=False):
        self.calls.append({"url": url, "params": params or {}, "stream": stream})
        return self.handler(url, params or {})


def _route(*rules):
    """rules: (子串, FakeResponse 或 payload dict)；按序匹配 URL。"""

    def handler(url, params):
        for needle, result in rules:
            if needle in url:
                return result if isinstance(result, FakeResponse) else FakeResponse(result)
        raise AssertionError(f"未打桩的 URL: {url}")

    return handler


def _use_session(monkeypatch, session: FakeSession) -> FakeSession:
    monkeypatch.setattr(video_mod, "_build_session", lambda: session)
    return session


# --- 1) URL 判定 ---------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://www.bilibili.com/video/BV1Q95N6rEXY",
        "https://www.bilibili.com/video/BV1Q95N6rEXY?p=2&t=1",
        "https://m.bilibili.com/video/BV1Q95N6rEXY",
        "https://www.bilibili.com/video/av170001",
    ],
)
def test_is_bilibili_video_url_positive(url):
    assert video_mod.is_bilibili_video_url(url) is True


@pytest.mark.parametrize(
    "url",
    ["https://www.douyin.com/video/123", "https://www.bilibili.com/", "", "https://www.bilibili.com/bangumi/play/x"],
)
def test_is_bilibili_video_url_negative(url):
    assert video_mod.is_bilibili_video_url(url) is False


# --- 2) ID 提取 ---------------------------------------------------------------

def test_extract_bv_id():
    assert video_mod.extract_bilibili_video_id("https://www.bilibili.com/video/BV1Q95N6rEXY") == BVID


def test_extract_bv_id_with_query():
    assert video_mod.extract_bilibili_video_id("https://www.bilibili.com/video/BV1Q95N6rEXY?p=2") == BVID


def test_extract_av_id():
    assert video_mod.extract_bilibili_video_id("https://www.bilibili.com/video/av170001") == "av170001"


def test_extract_invalid_returns_empty():
    assert video_mod.extract_bilibili_video_id("https://example.com/watch?v=abc") == ""
    assert video_mod.extract_bilibili_video_id("") == ""


# --- 3) CC 字幕 ---------------------------------------------------------------

def test_cc_subtitle_joins_cues_with_newline(monkeypatch):
    session = _use_session(
        monkeypatch,
        FakeSession(
            _route(
                ("player/wbi/v2", {"code": 0, "data": {"subtitle": {"subtitles": [{"lan": "zh", "subtitle_url": "//aisubtitle.test/sub.json"}]}}}),
                ("aisubtitle.test/sub.json", {"body": [{"content": "你好", "from": 0, "to": 1}, {"content": "世界", "from": 1, "to": 2}]}),
            )
        ),
    )

    text = video_mod.get_bilibili_cc_subtitle_text(BVID, 222)

    assert text == "你好\n世界"
    # 协议补全：请求 URL 必须是 https 开头
    assert any(c["url"].startswith("https://aisubtitle.test/sub.json") for c in session.calls)


def test_cc_subtitle_empty_returns_none(monkeypatch):
    _use_session(
        monkeypatch,
        FakeSession(_route(("player/wbi/v2", {"code": 0, "data": {"subtitle": {"subtitles": []}}}))),
    )
    assert video_mod.get_bilibili_cc_subtitle_text(BVID, 222) is None


# --- 4) 完整 mp4 直链 ---------------------------------------------------------

def _mp4_session(monkeypatch, *, duration: int, durl: list[dict]) -> None:
    _use_session(
        monkeypatch,
        FakeSession(
            _route(
                ("web-interface/view", {"code": 0, "data": {"cid": 222, "duration": duration}}),
                ("player/playurl", {"code": 0, "data": {"durl": durl}}),
            )
        ),
    )


def test_full_mp4_url_complete(monkeypatch):
    _mp4_session(monkeypatch, duration=100, durl=[{"url": "https://d.test/x.mp4", "length": 100_000}])
    assert video_mod.get_bilibili_full_mp4_url(BVID, 222) == "https://d.test/x.mp4"


def test_full_mp4_url_truncated_preview_returns_none(monkeypatch):
    _mp4_session(monkeypatch, duration=443, durl=[{"url": "https://d.test/x.mp4", "length": 35_000}])
    assert video_mod.get_bilibili_full_mp4_url(BVID, 222) is None


def test_full_mp4_url_no_durl_returns_none(monkeypatch):
    _mp4_session(monkeypatch, duration=100, durl=[])
    assert video_mod.get_bilibili_full_mp4_url(BVID, 222) is None


# --- 5) 下载 ------------------------------------------------------------------

def test_download_dash_merges_with_ffmpeg(monkeypatch, tmp_path):
    session = _use_session(
        monkeypatch,
        FakeSession(
            _route(
                ("web-interface/view", {"code": 0, "data": {"cid": 222, "duration": 90}}),
                (
                    "player/playurl",
                    {
                        "code": 0,
                        "data": {
                            "dash": {
                                "video": [
                                    {"baseUrl": "https://v.test/hi.m4s", "bandwidth": 900},
                                    {"baseUrl": "https://v.test/lo.m4s", "bandwidth": 100},
                                ],
                                "audio": [{"baseUrl": "https://a.test/lo.m4s", "bandwidth": 50}],
                            }
                        },
                    },
                ),
                ("v.test/lo.m4s", FakeResponse(content=b"VIDEO")),
                ("a.test/lo.m4s", FakeResponse(content=b"AUDIO")),
            )
        ),
    )
    run_calls: list[list[str]] = []

    monkeypatch.setattr(video_mod.shutil, "which", lambda name: f"C:/fake/{name}.exe")

    def fake_run(cmd, capture_output=True, text=True):
        run_calls.append(list(cmd))
        if cmd[0].endswith("ffprobe.exe"):
            return SimpleNamespace(returncode=0, stdout=json.dumps({"format": {"duration": "90.0"}}), stderr="")
        # ffmpeg：按真实行为产出成片
        from pathlib import Path

        Path(cmd[-1]).write_bytes(b"MERGED")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video_mod.subprocess, "run", fake_run)

    out = video_mod.download_bilibili_video(BVID, 222, dest_dir=tmp_path)

    assert out == tmp_path / f"{BVID}.mp4"
    assert out.read_bytes() == b"MERGED"
    # 选最小带宽流，且合流参数正确
    assert any(c["url"] == "https://v.test/lo.m4s" for c in session.calls)
    ffmpeg_cmd = next(c for c in run_calls if c[0].endswith("ffmpeg.exe"))
    assert "-c" in ffmpeg_cmd and "copy" in ffmpeg_cmd
    assert any(c[0].endswith("ffprobe.exe") for c in run_calls)
    # 中间 m4s 已清理
    assert not (tmp_path / f"{BVID}.video.m4s").exists()
    assert not (tmp_path / f"{BVID}.audio.m4s").exists()


def test_download_durl_single_file(monkeypatch, tmp_path):
    _use_session(
        monkeypatch,
        FakeSession(
            _route(
                ("web-interface/view", {"code": 0, "data": {"cid": 222, "duration": 90}}),
                ("player/playurl", {"code": 0, "data": {"durl": [{"url": "https://d.test/x.mp4", "length": 90_000}]}}),
                ("d.test/x.mp4", FakeResponse(content=b"MP4DATA")),
            )
        ),
    )

    out = video_mod.download_bilibili_video(BVID, 222, dest_dir=tmp_path)

    assert out == tmp_path / f"{BVID}.mp4"
    assert out.read_bytes() == b"MP4DATA"


# --- 6) 接口错误 --------------------------------------------------------------

def test_api_error_raises_runtime_error(monkeypatch):
    _use_session(monkeypatch, FakeSession(_route(("web-interface/view", {"code": -404, "message": "啥都木有"}))))

    with pytest.raises(RuntimeError) as excinfo:
        video_mod.get_bilibili_video_meta(f"https://www.bilibili.com/video/{BVID}")

    message = str(excinfo.value)
    assert "code=-404" in message
    assert "啥都木有" in message

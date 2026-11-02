"""B站视频 connector 的离线回归（bsk 页内取数）。

不触真实网络/浏览器：page facade 与 HTTP 下载会话均打桩。
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


class FakeHttpSession:
    """只服务字幕 JSON 与 CDN 媒体流；按 URL 子串路由。"""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.trust_env = True

    def get(self, url, *, timeout=None, stream=False):
        self.calls.append({"url": url, "stream": stream})
        return self.handler(url)


class FakePage:
    """bsk page facade：按 evaluate 传入的 URL 路由 JSON 接口。"""

    def __init__(self, routes, *, url: str = "https://www.bilibili.com/watchlater/list") -> None:
        self.routes = routes
        self._url = url
        self.gotos: list[str] = []
        self.evaluate_calls: list[tuple] = []

    @property
    def url(self) -> str:
        return self._url

    def goto(self, url, wait_until=None):
        self.gotos.append(url)
        self._url = url

    def evaluate(self, js, arg=None, timeout=None):
        self.evaluate_calls.append((js, arg, timeout))
        target = json.loads(arg)["url"]
        for needle, payload in self.routes:
            if needle in target:
                return payload
        raise AssertionError(f"未打桩的接口: {target}")


def _route(*rules):
    def handler(url):
        for needle, result in rules:
            if needle in url:
                return result if isinstance(result, FakeResponse) else FakeResponse(result)
        raise AssertionError(f"未打桩的 URL: {url}")

    return handler


def _use_http(monkeypatch, routes, default=None):
    session = FakeHttpSession(_route(*routes)) if default is None else FakeHttpSession(lambda url: default)
    monkeypatch.setattr(video_mod, "_download_session", lambda: session)
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


# --- 3) 页内取数基础 ----------------------------------------------------------

def test_page_is_moved_to_stable_page_when_elsewhere():
    page = FakePage([], url="about:blank")

    video_mod._ensure_stable_page(page)

    assert page.gotos == [video_mod.BILIBILI_STABLE_PAGE_URL]


def test_page_already_on_bilibili_is_reused():
    page = FakePage([])

    video_mod._ensure_stable_page(page)

    assert page.gotos == []


def test_meta_reads_view_api_through_page():
    page = FakePage(
        [("web-interface/view", {"code": 0, "data": {"bvid": BVID, "aid": 1, "cid": 222, "title": "标题", "owner": {"name": "UP"}, "duration": 90}})]
    )

    meta = video_mod.get_bilibili_video_meta(f"https://www.bilibili.com/video/{BVID}", page=page)

    assert meta == video_mod.BilibiliVideoMeta(BVID, 1, 222, "标题", "UP", 90)
    assert page.evaluate_calls  # 真的走了页内 fetch


# --- 4) CC 字幕 ---------------------------------------------------------------

def test_cc_subtitle_joins_cues_with_newline(monkeypatch):
    page = FakePage(
        [("player/v2", {"code": 0, "data": {"subtitle": {"subtitles": [{"lan": "zh", "subtitle_url": "//aisubtitle.test/sub.json"}]}}})]
    )
    http = _use_http(
        monkeypatch,
        [("aisubtitle.test/sub.json", {"body": [{"content": "你好", "from": 0, "to": 1}, {"content": "世界", "from": 1, "to": 2}]})],
    )

    text = video_mod.get_bilibili_cc_subtitle_text(BVID, 222, page=page)

    assert text == "你好\n世界"
    # 协议补全：请求 URL 必须是 https 开头
    assert any(c["url"].startswith("https://aisubtitle.test/sub.json") for c in http.calls)


def test_cc_subtitle_empty_returns_none():
    page = FakePage([("player/v2", {"code": 0, "data": {"subtitle": {"subtitles": []}}})])

    assert video_mod.get_bilibili_cc_subtitle_text(BVID, 222, page=page) is None


# --- 5) 完整 mp4 直链 ---------------------------------------------------------

def _mp4_page(*, duration: int, durl: list[dict]) -> FakePage:
    return FakePage(
        [
            ("web-interface/view", {"code": 0, "data": {"cid": 222, "duration": duration}}),
            ("player/playurl", {"code": 0, "data": {"durl": durl}}),
        ]
    )


def test_full_mp4_url_complete():
    page = _mp4_page(duration=100, durl=[{"url": "https://d.test/x.mp4", "length": 100_000}])
    assert video_mod.get_bilibili_full_mp4_url(BVID, 222, page=page) == "https://d.test/x.mp4"


def test_full_mp4_url_truncated_preview_returns_none():
    page = _mp4_page(duration=443, durl=[{"url": "https://d.test/x.mp4", "length": 35_000}])
    assert video_mod.get_bilibili_full_mp4_url(BVID, 222, page=page) is None


def test_full_mp4_url_no_durl_returns_none():
    page = _mp4_page(duration=100, durl=[])
    assert video_mod.get_bilibili_full_mp4_url(BVID, 222, page=page) is None


# --- 6) 下载 ------------------------------------------------------------------

def test_download_dash_merges_with_ffmpeg(monkeypatch, tmp_path):
    page = FakePage(
        [
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
        ]
    )
    http = _use_http(
        monkeypatch,
        [
            ("v.test/lo.m4s", FakeResponse(content=b"VIDEO")),
            ("a.test/lo.m4s", FakeResponse(content=b"AUDIO")),
        ],
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

    out = video_mod.download_bilibili_video(BVID, 222, page=page, dest_dir=tmp_path)

    assert out == tmp_path / f"{BVID}.mp4"
    assert out.read_bytes() == b"MERGED"
    # 选最小带宽流，且合流参数正确
    assert any(c["url"] == "https://v.test/lo.m4s" for c in http.calls)
    ffmpeg_cmd = next(c for c in run_calls if c[0].endswith("ffmpeg.exe"))
    assert "-c" in ffmpeg_cmd and "copy" in ffmpeg_cmd
    assert any(c[0].endswith("ffprobe.exe") for c in run_calls)
    # 中间 m4s 已清理
    assert not (tmp_path / f"{BVID}.video.m4s").exists()
    assert not (tmp_path / f"{BVID}.audio.m4s").exists()


def test_download_durl_single_file(monkeypatch, tmp_path):
    page = _mp4_page(duration=90, durl=[{"url": "https://d.test/x.mp4", "length": 90_000}])
    _use_http(monkeypatch, [("d.test/x.mp4", FakeResponse(content=b"MP4DATA"))])

    out = video_mod.download_bilibili_video(BVID, 222, page=page, dest_dir=tmp_path)

    assert out == tmp_path / f"{BVID}.mp4"
    assert out.read_bytes() == b"MP4DATA"


# --- 7) 接口错误 --------------------------------------------------------------

def test_api_error_raises_runtime_error():
    page = FakePage([("web-interface/view", {"code": -404, "message": "啥都木有"})])

    with pytest.raises(RuntimeError) as excinfo:
        video_mod.get_bilibili_video_meta(f"https://www.bilibili.com/video/{BVID}", page=page)

    message = str(excinfo.value)
    assert "code=-404" in message
    assert "啥都木有" in message

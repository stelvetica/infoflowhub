"""抖音取直链兜底路径的回归测试（不触网络、不开浏览器）。

背景：取链有四条通道——用户脚本 DOM 标记 → 扩展网络缓冲 → resource timing → 页面 HTML。
只看 resource timing 时，「播放器没起播」和「播放中被 strict 策略拒 evaluate」都会
被误报成失败（现场即 2049 条里的 2099/1752）。
"""

from __future__ import annotations

import base64
import json

from connectors._shared.bsk_browser import BskError
from connectors.douyin.video import (
    DouyinVideoMedia,
    _media_urls_from_html,
    _media_urls_from_marker,
    _media_urls_from_network,
    _split_media_urls,
    resolve_douyin_video_media,
)

VIDEO_ID = "7664439615676275451"
URL = f"https://www.douyin.com/video/{VIDEO_ID}"
MUXED = "https://v26-web.douyinvod.com/abc/def/video/tos/cn/tos-cn-ve-15c000-ce/x/?a=1&amp;br=813&amp;mime_type=video_mp4"
MUXED_HTML = f'<video id="x"><source class="" src="{MUXED}" type=""></video><source src="{MUXED}">'
V_STREAM = "https://v26-web.douyinvod.com/aaa/video/tos/cn/x/media-video-avc1/?sig=1"
A_STREAM = "https://v26-web.douyinvod.com/bbb/video/tos/cn/x/media-audio-und-mp4a/?sig=2"


class FakePage:
    def __init__(
        self,
        html: str,
        *,
        evaluate_error: bool = False,
        network: list | None = None,
        network_after_nav: list | None = None,
    ) -> None:
        self._html = html
        self._evaluate_error = evaluate_error
        self._network = list(network or [])
        self._network_after_nav = network_after_nav or []
        self.navigations: list[str] = []

    def goto(self, url: str, wait_until: str | None = None, timeout: float | None = None) -> None:
        self.navigations.append(url)
        self._network.extend(self._network_after_nav)

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def evaluate(self, js: str):
        if self._evaluate_error:
            raise BskError("evaluate 失败 (exit=1, code=permission_denied)")
        return {"media": [], "all_count": 0}

    def content(self) -> str:
        return self._html

    def network_entries(self, **kwargs):
        return self._network


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def acquire_page(self) -> FakePage:
        return self._page


def test_split_marks_separate_video_and_audio():
    video, audio = _split_media_urls([
        "https://x/media-audio-und-mp4a/?a=1",
        "https://x/media-video-hvc1/?a=1",
    ])
    assert video == "https://x/media-video-hvc1/?a=1"
    assert audio == "https://x/media-audio-und-mp4a/?a=1"


def test_split_treats_unmarked_url_as_muxed_video():
    video, audio = _split_media_urls(["https://x/video/tos/cn/abc/"])
    assert video == "https://x/video/tos/cn/abc/"
    assert audio is None


def test_split_empty():
    assert _split_media_urls([]) == (None, None)


def test_html_source_url_is_unescaped_and_deduped():
    urls = _media_urls_from_html(FakePage(MUXED_HTML))
    assert urls == [MUXED.replace("&amp;", "&")]


def test_html_falls_back_to_any_douyinvod_url():
    html = '<link rel="dns-prefetch" href="//v26-web.douyinvod.com"><script>x="https:\\u0026"</script>' + (
        '<meta content="https://v11-weba.douyinvod.com/a/b/video/tos/cn/y/?a=1\\u0026b=2">'
    )
    assert _media_urls_from_html(FakePage(html)) == [
        "https://v11-weba.douyinvod.com/a/b/video/tos/cn/y/?a=1&b=2"
    ]


def test_html_unreadable_returns_empty():
    class BrokenPage(FakePage):
        def content(self) -> str:
            raise BskError("get-html failed")

    assert _media_urls_from_html(BrokenPage("")) == []


def test_resolve_falls_back_to_html_when_evaluate_denied():
    # 真实故障现场：evaluate 被 strict 策略全拒，HTML 里的直链仍可用。
    media = resolve_douyin_video_media(URL, session=FakeSession(FakePage(MUXED_HTML, evaluate_error=True)))
    assert media == DouyinVideoMedia(video_url=MUXED.replace("&amp;", "&"), audio_url=None)


def test_resolve_raises_when_no_link_anywhere():
    try:
        resolve_douyin_video_media(URL, session=FakeSession(FakePage("<video></video>", evaluate_error=True)))
    except RuntimeError as exc:
        assert "网络缓冲/resource timing/HTML 均无媒体链接" in str(exc)
        assert "permission_denied" in str(exc)  # 失败原因不能丢
    else:  # pragma: no cover
        raise AssertionError("无直链时必须抛 RuntimeError")


# --- 网络缓冲通道 -------------------------------------------------------------

OLD_V = "https://v26-web.douyinvod.com/old/.../media-video-avc1/?sig=old"


def test_network_prefers_newest_streams_and_skips_failed():
    entries = [
        {"sequence": 10, "url": OLD_V, "status": 206},
        {"sequence": 30, "url": V_STREAM, "status": 206},
        {"sequence": 32, "url": V_STREAM, "status": 206},  # 同一路径重复请求
        {"sequence": 31, "url": A_STREAM, "status": 206},
        {"sequence": 33, "url": "https://x.douyinvod.com/bad/media-video-avc1/?sig=403", "status": 403},
    ]
    urls = _media_urls_from_network(FakePage("", network=entries))
    assert urls == [V_STREAM, A_STREAM, OLD_V]


def test_network_excludes_links_seen_before_navigation():
    entries = [{"sequence": 1, "url": OLD_V, "status": 206}]
    assert _media_urls_from_network(FakePage("", network=entries), exclude={OLD_V}) == []


def test_network_unavailable_returns_empty():
    class BrokenPage(FakePage):
        def network_entries(self, **kwargs):
            raise BskError("network 失败")

    assert _media_urls_from_network(BrokenPage("")) == []


def test_resolve_uses_network_when_evaluate_denied_and_html_empty():
    # 真实故障现场（2099）：视频播放中整页基线一直在变 → evaluate 全拒、HTML 也没有
    # <source>（MSE），但网络缓冲里明明有播放器发出的两条流。
    page = FakePage(
        "",
        evaluate_error=True,
        network_after_nav=[
            {"sequence": 407, "url": V_STREAM, "status": 206},
            {"sequence": 406, "url": A_STREAM, "status": 206},
        ],
    )
    media = resolve_douyin_video_media(URL, session=FakeSession(page))
    assert media == DouyinVideoMedia(video_url=V_STREAM, audio_url=A_STREAM)


def test_resolve_does_not_reuse_links_from_before_navigation():
    # 本条视频一个请求都没发时，缓冲里上一条视频的直链不能当成本条的结果
    page = FakePage("", network=[{"sequence": 10, "url": OLD_V, "status": 206}], network_after_nav=[])
    try:
        resolve_douyin_video_media(URL, session=FakeSession(page))
    except RuntimeError as exc:
        assert "均无媒体链接" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("旧链接不能冒充本条视频")


class FlakyNavPage(FakePage):
    """第 fail_from_goto 次导航起全部被拒（模拟 strict 策略拒绝重复导航）。"""

    def __init__(self, html: str, *, fail_from_goto: int) -> None:
        super().__init__(html)
        self._fail_from = fail_from_goto

    def goto(self, url: str, wait_until: str | None = None, timeout: float | None = None) -> None:
        self.navigations.append(url)
        if len(self.navigations) >= self._fail_from:
            raise BskError("navigate 失败 (exit=1, code=permission_denied)")


def test_resolve_keeps_loaded_page_when_second_navigation_denied():
    # 真实故障现场：首跳成功、二次导航被 strict 拒——页面已在目标页，应走 HTML 兜底。
    media = resolve_douyin_video_media(
        URL, session=FakeSession(FlakyNavPage(MUXED_HTML, fail_from_goto=2))
    )
    assert media == DouyinVideoMedia(video_url=MUXED.replace("&amp;", "&"), audio_url=None)


def test_resolve_raises_when_first_navigation_denied():
    # 首跳失败时页面可能停在任意页：绝不能拿它的 HTML 当目标视频。
    try:
        resolve_douyin_video_media(
            URL, session=FakeSession(FlakyNavPage(MUXED_HTML, fail_from_goto=1))
        )
    except BskError:
        pass
    else:  # pragma: no cover
        raise AssertionError("首跳失败必须抛错")


# --- 用户脚本 DOM 标记通道 -----------------------------------------------------

MARKER_URL = "https://v26-web.douyinvod.com/mark/.../video/tos/cn/y/?a=1&br=1141"


def _marker_html(entries: list[dict], current: dict | None = None) -> str:
    payload = {"entries": entries}
    if current is not None:
        payload["current"] = current
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    packed = base64.b64encode(raw).decode()
    return f'<html><head><meta id="ifh-douyin-media" name="ifh-douyin-media" content="{packed}"></head></html>'


def test_marker_returns_play_urls_for_matching_video():
    html = _marker_html([{"id": VIDEO_ID, "desc": "x", "playUrls": [MARKER_URL], "gears": []}])
    assert _media_urls_from_marker(FakePage(html), VIDEO_ID) == [MARKER_URL]


def test_marker_ignores_other_videos_and_unsafe_hosts():
    html = _marker_html([
        {"id": "999", "playUrls": ["https://v26-web.douyinvod.com/other/"]},
        {"id": VIDEO_ID, "playUrls": [
            "http://evil.example/v.mp4",
            "javascript:alert(1)",
            "https://evil.douyinvod.com.evil.example/v.mp4",
            MARKER_URL,
        ]},
    ])
    assert _media_urls_from_marker(FakePage(html), VIDEO_ID) == [MARKER_URL]


def test_marker_tolerates_broken_payload():
    broken = '<meta id="ifh-douyin-media" content="@@not-base64@@">'
    assert _media_urls_from_marker(FakePage(broken), VIDEO_ID) == []


def test_resolve_prefers_marker_over_network_and_evaluate():
    # 主通道命中后不应再碰 evaluate/network（真实页面里 evaluate 会被 strict 反复拒）
    page = FakePage(
        _marker_html([{"id": VIDEO_ID, "playUrls": [MARKER_URL]}]),
        evaluate_error=True,
        network_after_nav=[{"sequence": 9, "url": V_STREAM, "status": 206}],
    )
    media = resolve_douyin_video_media(URL, session=FakeSession(page))
    assert media == DouyinVideoMedia(video_url=MARKER_URL, audio_url=None)


def test_resolve_falls_back_to_network_when_marker_is_for_other_video():
    page = FakePage(
        _marker_html([{"id": "123", "playUrls": [MARKER_URL]}]),
        evaluate_error=True,
        network_after_nav=[{"sequence": 9, "url": V_STREAM, "status": 206}],
    )
    media = resolve_douyin_video_media(URL, session=FakeSession(page))
    assert media.video_url == V_STREAM


def test_marker_finds_current_even_after_entries_evicted():
    # 线上复现：相关推荐把当前视频挤出 entries，current 槽位仍保留它
    noise = [{"id": f"70000{i}", "playUrls": [f"https://v26-web.douyinvod.com/{i}/v/"]} for i in range(5)]
    html = _marker_html(noise, current={"id": VIDEO_ID, "playUrls": [MARKER_URL]})
    assert _media_urls_from_marker(FakePage(html), VIDEO_ID) == [MARKER_URL]

"""抖音取直链兜底路径的回归测试（不触网络、不开浏览器）。

背景：播放器没起播时 resource timing 里一条媒体请求都没有，但页面 HTML 的 <source src>
本来就带签名直链。此前只看 resource timing，于是「取不到」被误报成风控失败。
"""

from __future__ import annotations

from connectors._shared.bsk_browser import BskError
from connectors.douyin.video import (
    DouyinVideoMedia,
    _media_urls_from_html,
    _split_media_urls,
    resolve_douyin_video_media,
)

VIDEO_ID = "7664439615676275451"
URL = f"https://www.douyin.com/video/{VIDEO_ID}"
MUXED = "https://v26-web.douyinvod.com/abc/def/video/tos/cn/tos-cn-ve-15c000-ce/x/?a=1&amp;br=813&amp;mime_type=video_mp4"
MUXED_HTML = f'<video id="x"><source class="" src="{MUXED}" type=""></video><source src="{MUXED}">'


class FakePage:
    def __init__(self, html: str, *, evaluate_error: bool = False) -> None:
        self._html = html
        self._evaluate_error = evaluate_error
        self.navigations: list[str] = []

    def goto(self, url: str, wait_until: str | None = None, timeout: float | None = None) -> None:
        self.navigations.append(url)

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def evaluate(self, js: str):
        if self._evaluate_error:
            raise BskError("evaluate 失败 (exit=1, code=permission_denied)")
        return {"media": [], "all_count": 0}

    def content(self) -> str:
        return self._html


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
        assert "resource timing 与 HTML 均无媒体链接" in str(exc)
        assert "permission_denied" in str(exc)  # 失败原因不能丢
    else:  # pragma: no cover
        raise AssertionError("无直链时必须抛 RuntimeError")


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

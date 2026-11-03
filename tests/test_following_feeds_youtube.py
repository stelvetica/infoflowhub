"""YouTube 订阅流（聚合源）：ytInitialData 解析、作者提取与 DOM 兜底。

假 page 打桩，不连真实 daemon 与 Chrome。
"""

from __future__ import annotations

import json

from apps.subscriptions.models import FeedFetchResult
from connectors._shared import web_fetch
from connectors.youtube import feed as yt_feed
from connectors.youtube import fetch_youtube_subscriptions_with_page, fetch_youtube_with_page

SOURCE = {
    "id": "youtube-subscriptions",
    "name": "YouTube 关注",
    "feed_url": "https://www.youtube.com/feed/subscriptions",
    "site_url": "https://www.youtube.com/feed/subscriptions",
    "provider": "web",
    "fetch_via": "web",
}

CHANNEL_SOURCE = {
    "id": "some_channel",
    "name": "配置里的频道名",
    "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc",
    "site_url": "https://www.youtube.com/channel/UCabc",
    "provider": "web",
    "fetch_via": "web",
}


def _html(payload: dict | None) -> str:
    if payload is None:
        return "<html><body>no initial data</body></html>"
    return (
        "<html><script>var ytInitialData = "
        + json.dumps(payload, ensure_ascii=False)
        + ";</script></html>"
    )


def _video_renderer(
    video_id: str = "vidowner01",
    *,
    title: str = "订阅流标题",
    owner: str | None = None,
    short: str | None = None,
    long: str | None = None,
) -> dict:
    renderer = {
        "videoId": video_id,
        "title": {"runs": [{"text": title}]},
        "publishedTimeText": {"simpleText": "2 hours ago"},
        "viewCountText": {"simpleText": "1.2K views"},
    }
    if owner is not None:
        renderer["ownerText"] = {"runs": [{"text": owner}]}
    if short is not None:
        renderer["shortBylineText"] = {"runs": [{"text": short}]}
    if long is not None:
        renderer["longBylineText"] = {"runs": [{"text": long}]}
    return renderer


def _rich_grid(renderer: dict) -> dict:
    return {
        "contents": {
            "richGridRenderer": {
                "contents": [
                    {"richItemRenderer": {"content": {"videoRenderer": renderer}}}
                ]
            }
        }
    }


def _lockup(channel: str | None = "频道乙", *, video_id: str = "jjcfvreYmVw") -> dict:
    channel_part: dict = {"text": {"content": channel or ""}}
    if channel:
        channel_part["commandRuns"] = [
            {
                "onTap": {
                    "innertubeCommand": {
                        "browseEndpoint": {"canonicalBaseUrl": "/@channelb"}
                    }
                }
            }
        ]
    return {
        "contents": {
            "richGridRenderer": {
                "contents": [
                    {
                        "richItemRenderer": {
                            "content": {
                                "lockupViewModel": {
                                    "contentId": video_id,
                                    "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                                    "metadata": {
                                        "lockupMetadataViewModel": {
                                            "title": {"content": "订阅流锁标题"},
                                            "metadata": {
                                                "contentMetadataViewModel": {
                                                    "metadataRows": [
                                                        {
                                                            "metadataParts": [
                                                                channel_part,
                                                                {"text": {"content": "13K"}},
                                                                {"text": {"content": "11h ago"}},
                                                            ]
                                                        }
                                                    ]
                                                }
                                            },
                                        }
                                    },
                                }
                            }
                        }
                    }
                ]
            }
        }
    }


class _BaseSubscriptionsPage:
    def __init__(
        self,
        html: str,
        *,
        url: str = "https://www.youtube.com/feed/subscriptions",
    ) -> None:
        self._html = html
        self.url = url
        self.goto_url = ""

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        return None

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, timeout=None, state="visible") -> None:
        return None

    def content(self) -> str:
        return self._html


class FakeSubscriptionsPage(_BaseSubscriptionsPage):
    """带 DOM 兜底的假订阅页；dom_rows=None 时 evaluate_all 返回空。"""

    def __init__(
        self,
        html: str,
        *,
        url: str = "https://www.youtube.com/feed/subscriptions",
        dom_rows: list[dict] | None = None,
    ) -> None:
        super().__init__(html, url=url)
        self.dom_rows = dom_rows or []
        self.evaluate_all_calls = 0

    def evaluate_all(self, js, arg=None, **kwargs):
        self.evaluate_all_calls += 1
        return self.dom_rows


class FakeNoEvaluatePage(_BaseSubscriptionsPage):
    """fake page 没有 evaluate_all：DOM 兜底必须防御性跳过。"""


class FakeChannelPage:
    """逐频道源用的最小 page。"""

    def __init__(self, html: str) -> None:
        self._html = html
        self.goto_url = ""

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def wait_for_timeout(self, ms) -> None:
        return None

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, timeout=None, state="visible") -> None:
        return None

    def content(self) -> str:
        return self._html


# ---- 解析 ----


def test_subscriptions_uses_video_renderer_owner_text_as_source_name():
    page = FakeSubscriptionsPage(
        _html(_rich_grid(_video_renderer(owner="频道甲")))
    )

    result = fetch_youtube_subscriptions_with_page(page, SOURCE)

    assert result.ok, result.error
    assert len(result.entries) == 1
    assert result.entries[0].source_name == "频道甲"
    assert result.entries[0].link == "https://www.youtube.com/watch?v=vidowner01"
    assert "hl=en" in page.goto_url and "gl=US" in page.goto_url


def test_subscriptions_lockup_channel_browse_endpoint():
    page = FakeSubscriptionsPage(_html(_lockup("频道乙")))

    result = fetch_youtube_subscriptions_with_page(page, SOURCE)

    assert result.ok, result.error
    assert result.entries[0].source_name == "频道乙"


def test_subscriptions_dom_fallback_fills_missing_author():
    page = FakeSubscriptionsPage(
        _html(_rich_grid(_video_renderer())),
        dom_rows=[{"videoId": "vidowner01", "channelName": "频道丙"}],
    )

    result = fetch_youtube_subscriptions_with_page(page, SOURCE)

    assert result.ok, result.error
    assert result.entries[0].source_name == "频道丙"
    assert page.evaluate_all_calls == 1


def test_subscriptions_falls_back_to_source_name_without_author():
    page = FakeNoEvaluatePage(_html(_rich_grid(_video_renderer())))

    result = fetch_youtube_subscriptions_with_page(page, SOURCE)

    assert result.ok, result.error
    assert result.entries[0].source_name == SOURCE["name"]


def test_channel_source_keeps_configured_name():
    """逐频道源：即使 renderer 带 ownerText，也不改 source_name。"""
    page = FakeChannelPage(_html(_rich_grid(_video_renderer(owner="频道甲"))))

    result = fetch_youtube_with_page(page, CHANNEL_SOURCE)

    assert result.ok, result.error
    assert result.entries[0].source_name == CHANNEL_SOURCE["name"]


# ---- 登录 / 空内容 ----


def test_subscriptions_redirect_to_login_reports_login_state():
    page = FakeSubscriptionsPage(
        _html(_rich_grid(_video_renderer(owner="频道甲"))),
        url="https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com/",
    )

    result = fetch_youtube_subscriptions_with_page(page, SOURCE)

    assert not result.ok
    assert "登录态" in result.error


def test_subscriptions_missing_initial_data_reports_login_state():
    page = FakeSubscriptionsPage(_html(None))

    result = fetch_youtube_subscriptions_with_page(page, SOURCE)

    assert not result.ok
    assert "登录态" in result.error


def test_subscriptions_empty_initial_data_reports_empty_content():
    """有 renderer 结构但解析不出条目 → 空内容文案（区别于登录态）。"""
    payload = {"contents": {"richGridRenderer": {"contents": []}}}
    payload["contents"]["richGridRenderer"]["contents"].append(
        {
            "richItemRenderer": {
                "content": {
                    "videoRenderer": {
                        "videoId": "",
                        "title": {"runs": []},
                    }
                }
            }
        }
    )
    page = FakeSubscriptionsPage(_html(payload))

    result = fetch_youtube_subscriptions_with_page(page, SOURCE)

    assert not result.ok
    assert "未解析到可入库内容" in result.error


# ---- shorts ----


def test_extract_video_id_from_shorts_url():
    assert (
        yt_feed.extract_video_id_from_url("https://www.youtube.com/shorts/abcDEF12345")
        == "abcDEF12345"
    )


# ---- 分派 ----


class FakePageWithClose:
    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.page = FakePageWithClose()

    def acquire_page(self):
        return self.page


def test_subscriptions_routes_to_new_fetch_fn(monkeypatch):
    calls: list[str] = []

    def fake_fetch(page, source, timeout_ms=60000, limit=12):
        calls.append(source["id"])
        return FeedFetchResult(
            source["id"], source["name"], source["feed_url"], True, 200, []
        )

    monkeypatch.setattr(web_fetch, "fetch_youtube_subscriptions_with_page", fake_fetch)

    assert web_fetch._needs_shared_runner(SOURCE) is True
    result = web_fetch._fetch_one_via_session(FakeSession(), SOURCE)

    assert calls == [SOURCE["id"]]
    assert result.ok

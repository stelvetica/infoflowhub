"""关注流（聚合源）：站点识别、渠道标签、B 站 feed/all 页内取数。

假 page 打桩，不连真实 daemon 与 Chrome。
"""

from __future__ import annotations

import json

from connectors._shared.common import (
    AGGREGATE_SITES,
    BILIBILI_DYNAMIC_SITES,
    is_aggregate_source,
    resolve_web_target,
)
from connectors.bilibili import api as bili_api
from connectors.bilibili import fetch_bilibili_following_feed_with_page
from web.services.utils import source_channel_label

BILIBILI_FOLLOWING_SOURCE = {
    "id": "bilibili-following",
    "name": "B站关注",
    "feed_url": "https://t.bilibili.com/",
    "site_url": "https://t.bilibili.com/",
    "provider": "web",
    "fetch_via": "web",
}


# ---- 站点识别 ----


def test_resolve_web_target_recognizes_bilibili_following():
    target = resolve_web_target({"site_url": "https://t.bilibili.com/"})

    assert target is not None
    assert target.site == "bilibili_following"
    assert target.uid == ""
    assert target.page_url == "https://t.bilibili.com/"


def test_resolve_web_target_recognizes_youtube_subscriptions():
    target = resolve_web_target({"site_url": "https://www.youtube.com/feed/subscriptions"})

    assert target is not None
    assert target.site == "youtube_subscriptions"
    assert target.uid == ""
    assert target.page_url == "https://www.youtube.com/feed/subscriptions"


def test_resolve_web_target_keeps_existing_urls_unchanged():
    cases = [
        ("https://space.bilibili.com/424405794/dynamic", "bilibili", "424405794"),
        ("https://www.youtube.com/channel/UCabc/videos", "youtube", "UCabc"),
        ("https://www.youtube.com/@someone", "youtube", "@someone"),
        ("https://www.youtube.com/c/oldname", "youtube", "oldname"),
        ("https://www.youtube.com/user/legacy", "youtube", "legacy"),
    ]
    for url, site, uid in cases:
        target = resolve_web_target({"site_url": url})
        assert target is not None, url
        assert (target.site, target.uid) == (site, uid), url


def test_youtube_plain_channel_url_is_not_subscriptions():
    target = resolve_web_target({"site_url": "https://www.youtube.com/feed/trending"})

    assert target is None


def test_aggregate_constants():
    assert AGGREGATE_SITES == frozenset({"bilibili_following", "youtube_subscriptions"})
    assert BILIBILI_DYNAMIC_SITES == frozenset({"bilibili", "bilibili_following"})
    assert is_aggregate_source({"site_url": "https://t.bilibili.com/"}) is True
    assert is_aggregate_source({"site_url": "https://www.youtube.com/feed/subscriptions"}) is True
    assert is_aggregate_source({"site_url": "https://space.bilibili.com/1/dynamic"}) is False
    assert is_aggregate_source({"site_url": "https://www.youtube.com/channel/UCabc"}) is False


def test_source_channel_label_for_following_urls():
    assert source_channel_label("https://t.bilibili.com/", "", "web") == "Bilibili"
    assert (
        source_channel_label("", "https://www.youtube.com/feed/subscriptions", "web")
        == "YouTube"
    )


# ---- B 站关注流页内取数 ----


class FakeFollowingPage:
    def __init__(self, payloads) -> None:
        self.payloads = list(payloads)
        self.goto_url = ""
        self.evaluated: list = []

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def evaluate(self, js, arg=None, **kwargs):
        index = len(self.evaluated)
        self.evaluated.append((js, arg, kwargs))
        return self.payloads[min(index, len(self.payloads) - 1)]


def _item(n: int, *, author: str | None = "作者A", title: str | None = None) -> dict:
    module_author = {"pub_ts": "1789830900", "pub_time": "2026年09月19日 23:15", "pub_action": "发布了动态"}
    if author is not None:
        module_author["name"] = author
    return {
        "id_str": f"id{n}",
        "id": f"id{n}",
        "type": "DYNAMIC_TYPE_DRAW",
        "basic": {"jump_url": f"//t.bilibili.com/id{n}"},
        "modules": {
            "module_author": module_author,
            "module_dynamic": {
                "desc": {"text": title or f"关注动态 {n}"},
                "major": {"opus": {"summary": {}}},
            },
        },
    }


def _payload(items: list, *, offset: str = "", has_more: bool = False, code: int = 0, message: str = "0") -> dict:
    return {"code": code, "message": message, "items": items, "offset": offset, "has_more": has_more}


def test_following_feed_navigates_and_calls_feed_all():
    page = FakeFollowingPage([_payload([_item(1)])])

    fetch_bilibili_following_feed_with_page(page, BILIBILI_FOLLOWING_SOURCE)

    assert page.goto_url == "https://t.bilibili.com/"
    _js, arg, _kwargs = page.evaluated[0]
    args = json.loads(arg)
    assert args["apiPath"] == "all"
    assert args["apiType"] == "all"
    assert args["uid"] == ""


def test_following_feed_uses_author_name_per_entry():
    page = FakeFollowingPage(
        [_payload([_item(1, author="作者A"), _item(2, author="作者B")])]
    )

    result = fetch_bilibili_following_feed_with_page(page, BILIBILI_FOLLOWING_SOURCE)

    assert result.ok and len(result.entries) == 2
    assert [entry.source_name for entry in result.entries] == ["作者A", "作者B"]
    assert result.source_name == "B站关注"


def test_following_feed_falls_back_to_source_name_without_author():
    page = FakeFollowingPage([_payload([_item(1, author=None)])])

    result = fetch_bilibili_following_feed_with_page(page, BILIBILI_FOLLOWING_SOURCE)

    assert result.entries[0].source_name == "B站关注"


def test_following_feed_api_error_reports_login_expired():
    page = FakeFollowingPage([_payload([], code=-101, message="账号未登录")])

    result = fetch_bilibili_following_feed_with_page(page, BILIBILI_FOLLOWING_SOURCE)

    assert not result.ok
    assert "登录态失效" in result.error
    assert "-101" in result.error


def test_following_feed_limit_truncates_and_stops():
    items = [_item(n) for n in range(1, 5)]
    page = FakeFollowingPage([_payload(items, offset="next", has_more=True)])

    result = fetch_bilibili_following_feed_with_page(page, BILIBILI_FOLLOWING_SOURCE, limit=2)

    assert len(page.evaluated) == 1
    assert len(result.entries) == 2
    assert result.stop_reason == bili_api.BILIBILI_STOP_LIMIT


def test_following_feed_stops_on_duplicate_streak():
    items = [_item(1, title="旧 A"), _item(2, title="旧 B"), _item(3, title="旧 C")]
    page = FakeFollowingPage([_payload(items, offset="next", has_more=True)])
    source = {**BILIBILI_FOLLOWING_SOURCE, "known_entry_keys": ["旧 a", "旧 b", "旧 c"]}

    result = fetch_bilibili_following_feed_with_page(page, source)

    assert len(page.evaluated) == 1
    assert result.stop_reason == bili_api.BILIBILI_STOP_DUPLICATE_STREAK


def test_following_feed_error_keeps_bracket_format():
    page = FakeFollowingPage([_payload([], code=-352, message="风控校验失败")])

    result = fetch_bilibili_following_feed_with_page(page, BILIBILI_FOLLOWING_SOURCE)

    assert "B站抓取失败[following][stage=bsk][kind=api]" in result.error
    assert "code=-352" in result.error

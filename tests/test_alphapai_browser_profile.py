from __future__ import annotations

from connectors.rss import fetch as rss_fetch
from apps.subscriptions.models import FeedFetchResult


def test_fetch_many_uses_alphapai_limit(monkeypatch):
    """fetch_many 对 alphapai 源传入 FETCH_SOURCE_LIMIT。"""
    seen_limits = []

    def fake_fetch_alphapai_source(source, *, limit, session=None):
        seen_limits.append(limit)
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source["feed_url"],
            ok=True,
            status=200,
            entries=[],
        )

    monkeypatch.setattr(rss_fetch, "fetch_alphapai_source", fake_fetch_alphapai_source)

    rss_fetch.fetch_many(
        [
            {
                "id": "alphapai",
                "name": "Alpha",
                "feed_url": "https://alphapai-web.rabyte.cn/reading/home/market-report/detail",
                "provider": "web",
            }
        ]
    )

    assert seen_limits == [60]

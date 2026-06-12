from __future__ import annotations

from dataclasses import dataclass
from typing import List

from infra.text_normalizer import normalize_utf8_text


@dataclass
class FeedEntry:
    source_id: str
    source_name: str
    title: str
    link: str
    published: str
    summary: str
    markdown_path: str = ""

    def __post_init__(self) -> None:
        self.source_id = normalize_utf8_text(self.source_id)
        self.source_name = normalize_utf8_text(self.source_name)
        self.title = normalize_utf8_text(self.title)
        self.link = str(self.link or "").strip()
        self.published = normalize_utf8_text(self.published)
        self.summary = normalize_utf8_text(self.summary)
        self.markdown_path = str(self.markdown_path or "").strip()


@dataclass
class FeedFetchResult:
    source_id: str
    source_name: str
    feed_url: str
    ok: bool
    status: int
    entries: List[FeedEntry]
    error: str = ""

    def __post_init__(self) -> None:
        self.source_id = normalize_utf8_text(self.source_id)
        self.source_name = normalize_utf8_text(self.source_name)
        self.feed_url = str(self.feed_url or "").strip()
        self.error = normalize_utf8_text(self.error)
        self.entries = list(self.entries or [])


@dataclass
class SourceItem:
    id: str
    name: str
    group: str
    feed_url: str
    site_url: str
    provider: str
    fetch_via: str
    kind: str
    enabled: bool
    note: str
    channel: str = "rss"
    auth_key: str = ""
    fallback_mode: str = "none"

    def __post_init__(self) -> None:
        self.id = normalize_utf8_text(self.id)
        self.name = normalize_utf8_text(self.name)
        self.group = normalize_utf8_text(self.group)
        self.feed_url = str(self.feed_url or "").strip()
        self.site_url = str(self.site_url or "").strip()
        self.provider = str(self.provider or "").strip()
        self.fetch_via = str(self.fetch_via or "").strip()
        self.kind = str(self.kind or "").strip()
        self.note = normalize_utf8_text(self.note)
        self.channel = str(self.channel or "rss").strip()
        self.auth_key = str(self.auth_key or "").strip()
        self.fallback_mode = str(self.fallback_mode or "none").strip()

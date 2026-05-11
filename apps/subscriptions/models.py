from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class FeedEntry:
    source_id: str
    source_name: str
    title: str
    link: str
    published: str
    summary: str


@dataclass
class FeedFetchResult:
    source_id: str
    source_name: str
    feed_url: str
    ok: bool
    status: int
    entries: List[FeedEntry]
    error: str = ""

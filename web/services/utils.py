from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from connectors._shared.common import resolve_web_target


def strip_invalid_unicode(value: str) -> str:
    return re.sub(r"[\ud800-\udfff]", "", value or "")


def normalize_text(value: str) -> str:
    return strip_invalid_unicode(value).strip().lower()


def split_tags(value: str | None) -> list[str]:
    text = strip_invalid_unicode(value or "").strip()
    if not text:
        return []
    parts = (
        text.replace("，", ",")
        .replace("、", ",")
        .replace("；", ",")
        .replace(";", ",")
        .replace("|", ",")
        .split(",")
    )
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = part.strip()
        if not item:
            continue
        key = normalize_text(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def join_tags(tags: list[str]) -> str:
    return ",".join(strip_invalid_unicode(item).strip() for item in tags if strip_invalid_unicode(item).strip())


def parse_datetime(value: str) -> datetime | None:
    text = strip_invalid_unicode(value or "").strip()
    if not text:
        return None
    normalized = text.replace("年", "/").replace("月", "/").replace("日", "").replace("  ", " ").strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized.replace(" ", "T").replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def format_datetime(value: str) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return strip_invalid_unicode(value or "").strip().replace("-", "/")[:16]
    return parsed.strftime("%Y/%m/%d %H:%M")


def format_date(value: str) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return strip_invalid_unicode(value or "").strip().replace("-", "/")[:10]
    return parsed.strftime("%Y/%m/%d")


def to_sortable_time(value: str) -> float:
    parsed = parse_datetime(value)
    return parsed.timestamp() if parsed else 0.0


def compare_value(a: Any, b: Any, direction: str) -> int:
    factor = 1 if direction == "asc" else -1
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a < b:
            return -1 * factor
        if a > b:
            return 1 * factor
        return 0
    a_text = str(a or "")
    b_text = str(b or "")
    if a_text < b_text:
        return -1 * factor
    if a_text > b_text:
        return 1 * factor
    return 0


def provider_label(provider: str, fetch_via: str) -> str:
    if provider == "rsshub":
        return "RSSHub 公共" if fetch_via == "rsshub-public" else "RSSHub"
    if provider == "web":
        return "网页直抓"
    return "原生 RSS"


def source_channel_label(feed_url: str, site_url: str, provider: str) -> str:
    target = resolve_web_target({"site_url": site_url, "feed_url": feed_url})
    if target:
        mapping = {
            "bilibili": "Bilibili",
            "weibo": "微博",
            "wechat": "微信公众号",
            "x": "X",
        }
        return mapping.get(target.site, target.site.upper())
    feed = normalize_text(feed_url)
    site = normalize_text(site_url)
    combined = f"{feed} {site}"
    if "youtube.com" in combined or "youtu.be" in combined:
        return "YouTube"
    if "wechat://mp/" in combined or "mp.weixin.qq.com" in combined:
        return "微信公众号"
    if "douyin.com" in combined:
        return "抖音"
    if provider == "rsshub" or "rsshub" in feed:
        if "bilibili" in feed:
            return "Bilibili"
        if "weibo" in feed:
            return "微博"
        if "twitter" in feed or "x.com" in feed:
            return "X"
        return "RSSHub"
    return "RSS"


def build_source_id(name: str) -> str:
    value = (
        strip_invalid_unicode(name)
        .strip()
        .lower()
        .replace("：", "-")
        .replace("（", "-")
        .replace("）", "-")
        .replace("(", "-")
        .replace(")", "-")
        .replace("/", "-")
        .replace("\\", "-")
        .replace(":", "-")
        .replace(" ", "-")
    )
    value = "-".join(part for part in value.split("-") if part)
    return value or f"source-{int(datetime.now().timestamp())}"

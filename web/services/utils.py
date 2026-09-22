from __future__ import annotations

import hashlib
import re
from datetime import datetime
from functools import cmp_to_key
from typing import Any
from urllib.parse import quote, unquote

from connectors._shared.common import resolve_web_target


def strip_invalid_unicode(value: str) -> str:
    return re.sub(r"[\ud800-\udfff]", "", value or "")


def normalize_text(value: str) -> str:
    return strip_invalid_unicode(value).strip().lower()


def read_link_key(value: str) -> str:
    text = strip_invalid_unicode(value or "").strip()
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def read_entry_key(source_id: str, link: str, title: str = "") -> str:
    normalized_source = strip_invalid_unicode(source_id or "").strip().lower()
    normalized_link = strip_invalid_unicode(link or "").strip()
    normalized_title = strip_invalid_unicode(title or "").strip()
    if normalized_source == "alphapai" and normalized_title:
        return read_link_key(f"{normalized_source}|{normalized_title}")
    return read_link_key(normalized_link)


def split_tags(value: str | None) -> list[str]:
    text = strip_invalid_unicode(value or "").strip()
    if not text:
        return []
    parts = (
        text.replace("，", ",")
        .replace("、", ",")
        .replace("；", ",")
        .replace("：", ",")
        .replace(":", ",")
        .replace("?", ",")
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


def sort_rows_with_tiebreak(rows: list[dict[str, Any]], sort: str, direction: str, identity_key: str) -> None:
    """排序并追加行身份作次级键:同一排序值内顺序确定,游标切片才精确。"""
    rows.sort(
        key=cmp_to_key(
            lambda a, b: compare_value(a.get(sort), b.get(sort), direction)
            or compare_value(a.get(identity_key), b.get(identity_key), direction)
        )
    )


def encode_cursor(key_value: Any, identity: Any) -> str:
    """把「排序键 + 行身份」编成可放进 URL 的游标。

    分隔符用 | :quote(safe='') 会把它编成 %7C,而 ~ 属 RFC 3986 非保留字符不会被编码,
    故只有 | 能保证两段里不出现裸分隔符。
    """
    return f"{quote(str(key_value if key_value is not None else ''), safe='')}|{quote(str(identity or ''), safe='')}"


def decode_cursor(cursor: str) -> tuple[str, str]:
    key_text, _, identity = str(cursor or "").rpartition("|")
    return unquote(key_text), unquote(identity)


def take_after_cursor(
    rows: list[dict[str, Any]],
    cursor: str,
    sort: str,
    direction: str,
    identity_key: str,
    batch_size: int,
) -> tuple[list[dict[str, Any]], str]:
    """游标续载:返回(本批行, 下一游标)。

    cursor 为空即取首批;下一游标为空表示已取完。游标锚在「最后一行的排序键+身份」上,
    因此列表被增删(如稍后处理完成一条)也不会跳条或重复——这是相对页码偏移的关键差别。
    """
    remaining = rows
    if cursor:
        key_text, identity = decode_cursor(cursor)

        def is_after(row: dict[str, Any]) -> bool:
            primary = compare_value(row.get(sort), key_text, direction)
            return primary > 0 or (primary == 0 and compare_value(row.get(identity_key), identity, direction) > 0)

        remaining = [row for row in rows if is_after(row)]

    batch = remaining[:batch_size]
    if not batch or len(remaining) <= len(batch):
        return batch, ""
    last = batch[-1]
    return batch, encode_cursor(last.get(sort), last.get(identity_key))


def provider_label(provider: str, fetch_via: str) -> str:
    if provider == "rsshub":
        return "RSSHub 公开" if fetch_via == "rsshub-public" else "RSSHub"
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
            "douyin": "抖音",
            "jiuyangongshe": "韭研公社",
        }
        return mapping.get(target.site, target.site.upper())
    feed = normalize_text(feed_url)
    site = normalize_text(site_url)
    combined = f"{feed} {site}"
    if "youtube.com" in combined or "youtu.be" in combined:
        return "YouTube"
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
        .replace("，", "-")
        .replace("、", "-")
        .replace("；", "-")
        .replace("(", "-")
        .replace(")", "-")
        .replace("/", "-")
        .replace("\\", "-")
        .replace(":", "-")
        .replace(" ", "-")
    )
    value = "-".join(part for part in value.split("-") if part)
    return value or f"source-{int(datetime.now().timestamp())}"

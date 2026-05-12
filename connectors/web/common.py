from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from apps.subscriptions.models import FeedFetchResult


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
BASE_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = BASE_DIR / "runtime"
BILIBILI_PROFILE_DIR = RUNTIME_DIR / "browser_profiles" / "pw-bili-profile"
WEIBO_PROFILE_DIR = RUNTIME_DIR / "browser_profiles" / "pw-weibo-profile"


@dataclass
class WebSourceTarget:
    site: str
    uid: str
    page_url: str


def resolve_web_target(source: dict) -> WebSourceTarget | None:
    site_url = (source.get("site_url") or "").strip()
    match = re.search(r"space\.bilibili\.com/(\d+)", site_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="bilibili", uid=uid, page_url=f"https://space.bilibili.com/{uid}/dynamic")
    match = re.search(r"weibo\.com/(\d+)", site_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="weibo", uid=uid, page_url=f"https://weibo.com/{uid}/")
    match = re.search(r"x\.com/([^/?#]+)", site_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="x", uid=uid, page_url=f"https://x.com/{uid}")
    return None


def clean_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def now_text() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M")


def fallback_published(value: str) -> str:
    return value or now_text()


def normalize_title_key(text: str) -> str:
    value = re.sub(r"\s+", "", text or "")
    value = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return value.lower()


def normalize_yearless_date(text: str) -> str:
    digits = re.findall(r"\d+", text or "")
    now = datetime.now()
    if len(digits) >= 3:
        year, month, day = map(int, digits[:3])
        return f"{year:04d}/{month:02d}/{day:02d} 00:00"
    if len(digits) >= 2:
        month, day = map(int, digits[:2])
        return f"{now.year:04d}/{month:02d}/{day:02d} 00:00"
    return ""


def normalize_relative_date(text: str) -> str:
    now = datetime.now()
    line = clean_line(text)
    if not line:
        return ""

    match = re.fullmatch(r"(\d+)\s*天前(?:\s+(\d{2}:\d{2}))?", line)
    if match:
        target = now - timedelta(days=int(match.group(1)))
        return target.strftime(f"%Y/%m/%d {match.group(2) or '00:00'}")

    match = re.fullmatch(r"(\d+)\s*小时前", line)
    if match:
        return (now - timedelta(hours=int(match.group(1)))).strftime("%Y/%m/%d %H:%M")

    match = re.fullmatch(r"(\d+)\s*分钟前", line)
    if match:
        return (now - timedelta(minutes=int(match.group(1)))).strftime("%Y/%m/%d %H:%M")

    match = re.fullmatch(r"昨天(?:\s+(\d{2}:\d{2}))?", line)
    if match:
        return (now - timedelta(days=1)).strftime(f"%Y/%m/%d {match.group(1) or '00:00'}")

    match = re.fullmatch(r"前天(?:\s+(\d{2}:\d{2}))?", line)
    if match:
        return (now - timedelta(days=2)).strftime(f"%Y/%m/%d {match.group(1) or '00:00'}")

    match = re.fullmatch(r"今天\s+(\d{2}:\d{2})", line)
    if match:
        return now.strftime(f"%Y/%m/%d {match.group(1)}")

    match = re.fullmatch(r"(\d{2}:\d{2})", line)
    if match:
        hour, minute = [int(part) for part in match.group(1).split(":")]
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return now.strftime(f"%Y/%m/%d {match.group(1)}")

    return ""


def normalize_english_date(text: str) -> str:
    line = (text or "").strip()
    if not line:
        return ""
    for pattern in ("%b %d, %Y", "%b %d", "%I:%M %p · %b %d, %Y"):
        try:
            dt = datetime.strptime(line, pattern)
            if pattern == "%b %d":
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y/%m/%d %H:%M")
        except ValueError:
            continue
    return ""


def parse_published_datetime(text: str) -> datetime | None:
    value = clean_line(text)
    if not value:
        return None

    for parser in (
        lambda item: datetime.strptime(item, "%Y/%m/%d %H:%M"),
        lambda item: datetime.strptime(item, "%Y/%m/%d %H:%M:%S"),
        lambda item: datetime.strptime(item, "%Y/%m/%d"),
        lambda item: datetime.strptime(item, "%Y-%m-%d %H:%M:%S"),
        lambda item: datetime.strptime(item, "%Y-%m-%d"),
        lambda item: datetime.fromisoformat(item.replace("Z", "+00:00")).replace(tzinfo=None),
        lambda item: parsedate_to_datetime(item).replace(tzinfo=None),
    ):
        try:
            return parser(value)
        except Exception:
            continue
    return None


def result_error(source: dict, error: str) -> FeedFetchResult:
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=source["feed_url"],
        ok=False,
        status=0,
        entries=[],
        error=error,
    )


def launch_bilibili_context(playwright, headless: bool):
    BILIBILI_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(BILIBILI_PROFILE_DIR),
        headless=headless,
        args=["--window-size=1440,960"],
        user_agent=USER_AGENT,
    )


def launch_weibo_context(playwright, headless: bool):
    WEIBO_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(WEIBO_PROFILE_DIR),
        headless=headless,
        args=["--window-size=1440,960"],
        user_agent=USER_AGENT,
    )

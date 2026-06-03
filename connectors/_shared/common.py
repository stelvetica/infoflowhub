from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from apps.subscriptions.models import FeedFetchResult
from connectors.auth import get_auth_context_path, validate_auth


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
WEIBO_PROFILE_DIR = get_auth_context_path("weibo_shared")
X_PROFILE_DIR = get_auth_context_path("x_profile2")
DOUYIN_PROFILE_DIR = get_auth_context_path("douyin_shared")
X_LOGIN_HINT = "请先在本机 Chrome 的 Profile 2 中登录 x.com，并确认 MacroMargin 时间线可正常加载。"
DOUYIN_LOGIN_HINT = "请先执行现有抖音登录流程，当前抖音订阅与抖音收藏共用同一份登录态。"


TRANSIENT_FETCH_ERROR_KEYWORDS = (
    "connection reset",
    "connectionreseterror",
    "connection aborted",
    "connection closed",
    "err_connection_closed",
    "unexpected eof",
    "eof occurred in violation of protocol",
    "ssl eof",
    "remote end closed connection",
    "temporarily unavailable",
    "net::err_",
    "timed out",
    "timeout",
    "10054",
)


@dataclass
class WebSourceTarget:
    site: str
    uid: str
    page_url: str


def is_macromargin_source(source: dict) -> bool:
    auth_key = str(source.get("auth_key") or "").strip().lower()
    if auth_key == "x_profile2":
        return True
    feed_url = str(source.get("feed_url") or "").strip().lower()
    site_url = str(source.get("site_url") or "").strip().lower()
    return feed_url == "https://rsshub.app/twitter/user/macromargin" or site_url == "https://x.com/macromargin"


def validate_x_login_prerequisite(source: dict) -> str:
    if not is_macromargin_source(source):
        return ""
    descriptor = validate_auth("x_profile2")
    return "" if descriptor.is_available else descriptor.hint


def validate_douyin_login_prerequisite(source: dict) -> str:
    auth_key = str(source.get("auth_key") or "").strip().lower()
    site_url = str(source.get("site_url") or "").strip().lower()
    feed_url = str(source.get("feed_url") or "").strip().lower()
    if auth_key != "douyin_shared" and "douyin.com/user/" not in site_url and "douyin.com/user/" not in feed_url:
        return ""
    descriptor = validate_auth("douyin_shared")
    return "" if descriptor.is_available else descriptor.hint


def resolve_web_target(source: dict) -> WebSourceTarget | None:
    site_url = (source.get("site_url") or "").strip()
    feed_url = (source.get("feed_url") or "").strip()
    if feed_url.startswith("wechat://mp/") or site_url.startswith("wechat://mp/"):
        raw = feed_url if feed_url.startswith("wechat://mp/") else site_url
        uid = raw.split("wechat://mp/", 1)[1].strip().strip("/")
        if uid:
            return WebSourceTarget(site="wechat", uid=uid, page_url=f"https://mp.weixin.qq.com/cgi-bin/appmsgpublish?fakeid={uid}")
    if feed_url.startswith("wechat://article?url=") or "mp.weixin.qq.com/s/" in site_url:
        return WebSourceTarget(site="wechat", uid="", page_url=site_url or feed_url)
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
    match = re.search(r"douyin\.com/user/([^/?#]+)", site_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="douyin", uid=uid, page_url=site_url.split("?", 1)[0])
    match = re.search(r"douyin\.com/user/([^/?#]+)", feed_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="douyin", uid=uid, page_url=feed_url.split("?", 1)[0])
    match = re.search(r"[?&]channel_id=([A-Za-z0-9_-]+)", feed_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="youtube", uid=uid, page_url=f"https://www.youtube.com/channel/{uid}/videos")
    match = re.search(r"youtube\.com/channel/([A-Za-z0-9_-]+)", site_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="youtube", uid=uid, page_url=f"https://www.youtube.com/channel/{uid}/videos")
    match = re.search(r"youtube\.com/(@[A-Za-z0-9._-]+)", site_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="youtube", uid=uid, page_url=f"https://www.youtube.com/{uid}/videos")
    match = re.search(r"youtube\.com/(?:c|user)/([A-Za-z0-9._-]+)", site_url)
    if match:
        uid = match.group(1)
        return WebSourceTarget(site="youtube", uid=uid, page_url=f"https://www.youtube.com/{site_url.rstrip('/').split('youtube.com/', 1)[1]}/videos")
    return None


def with_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: value for key, value in params.items() if value})
    return urlunparse(parsed._replace(query=urlencode(query)))


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


def is_transient_fetch_error(message: str) -> bool:
    text = clean_line(message).lower()
    if not text:
        return False
    return any(keyword in text for keyword in TRANSIENT_FETCH_ERROR_KEYWORDS)


def launch_weibo_context(playwright, headless: bool):
    WEIBO_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(WEIBO_PROFILE_DIR),
        headless=headless,
        args=["--window-size=1440,960"],
        user_agent=USER_AGENT,
    )


def launch_x_context(playwright, headless: bool):
    last_error: Exception | None = None
    for channel in ("chrome", None):
        try:
            kwargs = {
                "user_data_dir": str(X_PROFILE_DIR),
                "headless": headless,
                "args": [
                    "--window-size=1440,960",
                    "--disable-blink-features=AutomationControlled",
                ],
                "user_agent": USER_AGENT,
                "ignore_default_args": ["--enable-automation"],
            }
            if channel:
                kwargs["channel"] = channel
            return playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("X 浏览器上下文启动失败")

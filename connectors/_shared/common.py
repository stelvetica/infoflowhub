from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from apps.laterhub.config import PW_DOUYIN_PROFILE
from apps.subscriptions.models import FeedFetchResult


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
BASE_DIR = Path(__file__).resolve().parents[2]
WEIBO_PROFILE_DIR = BASE_DIR / "runtime" / "browser_profiles" / "pw-weibo-profile"
X_PROFILE_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Profile 2"
X_LOGIN_HINT = "请先在本机 Chrome 的 Profile 2 中登录 x.com，并确认 MacroMargin 时间线可正常加载。"
DOUYIN_LOGIN_HINT = "请先执行现有抖音登录流程，当前抖音订阅与抖音收藏共用同一份登录态。"


@dataclass
class WebSourceTarget:
    site: str
    uid: str
    page_url: str


def is_macromargin_source(source: dict) -> bool:
    auth_type = str(source.get("auth_type") or "").strip().lower()
    if auth_type == "chrome_profile_x":
        return True
    feed_url = str(source.get("feed_url") or "").strip().lower()
    site_url = str(source.get("site_url") or "").strip().lower()
    return feed_url == "https://rsshub.app/twitter/user/macromargin" or site_url == "https://x.com/macromargin"


def validate_x_login_prerequisite(source: dict) -> str:
    if not is_macromargin_source(source):
        return ""
    if not X_PROFILE_DIR.exists():
        return f"MacroMargin 依赖本机 Chrome Profile 2 登录态。当前未找到目录：{X_PROFILE_DIR}"
    cookies_candidates = [
        X_PROFILE_DIR / "Cookies",
        X_PROFILE_DIR / "Network" / "Cookies",
    ]
    missing: list[str] = []
    if not any(path.exists() for path in cookies_candidates):
        missing.append("Cookies")
    if not (X_PROFILE_DIR / "Preferences").exists():
        missing.append("Preferences")
    if missing:
        return f"MacroMargin 依赖本机 Chrome Profile 2 登录态。请先用该 Profile 登录 x.com，缺少关键文件：{', '.join(missing)}"
    return ""


def validate_douyin_login_prerequisite(source: dict) -> str:
    auth_type = str(source.get("auth_type") or "").strip().lower()
    site_url = str(source.get("site_url") or "").strip().lower()
    feed_url = str(source.get("feed_url") or "").strip().lower()
    if auth_type != "douyin_profile" and "douyin.com/user/" not in site_url and "douyin.com/user/" not in feed_url:
        return ""
    if not PW_DOUYIN_PROFILE.exists():
        return f"抖音订阅依赖共享登录态目录，当前未找到：{PW_DOUYIN_PROFILE}"
    state_candidates = [
        PW_DOUYIN_PROFILE / "Cookies",
        PW_DOUYIN_PROFILE / "Network" / "Cookies",
        PW_DOUYIN_PROFILE / "Preferences",
        PW_DOUYIN_PROFILE / "Local State",
    ]
    if not any(path.exists() for path in state_candidates):
        return f"抖音共享登录态目录存在，但未发现可用会话文件。{DOUYIN_LOGIN_HINT}"
    return ""


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
                "args": ["--window-size=1440,960"],
                "user_agent": USER_AGENT,
            }
            if channel:
                kwargs["channel"] = channel
            return playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("X 浏览器上下文启动失败")

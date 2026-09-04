from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import parse_published_datetime, result_error


def _format_timestamp(value: int) -> str:
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y/%m/%d %H:%M")


# 与 connectors/rss/fetch.py 的 trim_entries 保持同一约束（≥10 篇且覆盖 3 天）。
# 因 feed 经 web_fetch 间接引 rss.fetch 会循环导入，这里内联等价实现，不复刻依赖。
MIN_SOURCE_ENTRIES = 10
MIN_SOURCE_DAYS = 3


def _trim_entries(entries: list[FeedEntry]) -> list[FeedEntry]:
    if not entries:
        return entries
    sorted_entries = sorted(
        entries,
        key=lambda item: parse_published_datetime(item.published) or datetime.min,
        reverse=True,
    )
    threshold = datetime.now() - timedelta(days=MIN_SOURCE_DAYS)
    selected: list[FeedEntry] = []
    reached_min_count = False
    reached_min_days = False
    for item in sorted_entries:
        selected.append(item)
        published_dt = parse_published_datetime(item.published)
        if len(selected) >= MIN_SOURCE_ENTRIES:
            reached_min_count = True
        if published_dt and published_dt <= threshold:
            reached_min_days = True
        if reached_min_count and reached_min_days:
            break
    return selected if selected else sorted_entries


# 轻量模式：通过外部工具(wechat-mp-tools 微信读书)的 /api/articles/list/<fakeid>
# 直取文章元数据（标题/链接/摘要/时间），不下载正文。URL 形式：weread://<fakeid>@<host>
def _parse_weread_url(source: dict) -> tuple[str, str]:
    """解析 weread://<fakeid>@<host> 形式，返回 (host, fakeid)；格式不符返回 ("","")。"""
    for value in (str(source.get("feed_url") or ""), str(source.get("site_url") or "")):
        if not value.startswith("weread://"):
            continue
        rest = value[len("weread://"):]
        if "@" in rest:
            fakeid, host = rest.split("@", 1)
            return host.strip(), fakeid.strip()
        return rest.strip(), ""
    return "", ""


def _is_connection_refused(exc: Exception) -> bool:
    """识别"服务未监听"类错误（Windows WinError 10061 / ConnectionRefusedError）。"""
    return isinstance(exc, ConnectionRefusedError) or "10061" in str(exc) or "Connection refused" in str(exc)


def fetch_wechat_feed_via_tool(source: dict, limit: int = 30) -> FeedFetchResult:
    """走外部工具轻量 API，仅取标题/链接/摘要元数据，复用 trim_entries 截断（≥10篇且覆盖3天）。"""
    host, fakeid = _parse_weread_url(source)
    if not host or not fakeid:
        return result_error(source, "微信公众号轻量源缺少工具地址或 fakeid，请检查 weread:// 配置。")
    url = f"http://{host}/api/articles/list/{urllib.parse.quote(fakeid)}?count={max(int(limit), 10)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    # 中转平台（weread 中转域）对连续请求有分钟级软限流：HTTP 200 但静默返回空数组，
    # 实测隔约 1 分钟恢复。短间隔重试跨不过该窗口，需指数退避拖过限流期；
    # 末档 60s 覆盖实测限流窗口，全部重试耗尽仍为空才判定失败
    # （未收录与限流无法从响应区分，文案并列提示）。
    WEREAD_EMPTY_RETRY_DELAYS = (0.0, 5.0, 15.0, 30.0, 60.0)
    articles: list = []
    last_error = ""
    service_down = False
    for attempt, delay in enumerate(WEREAD_EMPTY_RETRY_DELAYS, start=1):
        if delay:
            import time
            time.sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
            articles = payload.get("articles", []) if isinstance(payload, dict) else []
            if isinstance(articles, list) and articles:
                break
            last_error = "微信公众号轻量接口返回空：可能触发中转平台软限流（约1分钟自愈），或该号未被微信读书收录。"
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            last_error = f"微信公众号轻量抓取失败: HTTP {exc.code} {body[:200]}"
            # 确定性业务失败（账号池空/登录失效），重试无意义，短路 110s 退避
            if "账号池" in body or "无可用账号" in body:
                break
        except Exception as exc:
            last_error = f"微信公众号轻量抓取失败(工具不可达?): {exc}"
            # 连接拒绝 = 工具服务未起，重试无意义，短路 110s 退避
            if _is_connection_refused(exc):
                service_down = True
                break
    if service_down:
        last_error = "微信公众号工具服务(127.0.0.1:5300)未运行，请重启 InfoFlowHub 服务或手动拉起 wechat-mp-tools。"
    if not isinstance(articles, list) or not articles:
        return result_error(source, last_error or "微信公众号轻量接口返回空。")
    entries: list[FeedEntry] = []
    for article in articles:
        title = str(article.get("title") or "").strip()
        link = str(article.get("link") or "").strip()
        if not title or not link:
            continue
        published = _format_timestamp(int(article.get("update_time") or 0))
        summary = str(article.get("digest") or "").strip()
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=published,
                summary=summary,
            )
        )
    trimmed = _trim_entries(entries)
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=source["feed_url"],
        ok=True,
        status=200,
        entries=trimmed,
        error="",
    )


def fetch_wechat_feed(source: dict, limit: int = 12) -> FeedFetchResult:
    # 公众号唯一通道：外部工具 wechat-mp-tools 微信读书轻量接口（weread:// 源）。
    # 旧公众号后台 appmsgpublish 接口已被微信下线（2026-07-30 起恒返 200013），不再回退。
    if not _parse_weread_url(source)[1]:
        return result_error(
            source,
            "微信公众号源缺少 weread://<fakeid>@<host> 配置，请改为外部工具微信读书通道。",
        )
    return fetch_wechat_feed_via_tool(source, limit=limit)

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import clean_line, normalize_english_date, now_text, parse_published_datetime, resolve_web_target, result_error, with_query_params

logger = logging.getLogger("youtube.feed")


_TIME_UNITS_AGO = re.compile(
    r"(\d+)\s*"
    r"(second|sec|secs|minute|minutes|min|mins|hour|hours|hr|hrs|hr\.|hrs\.|day|days|week|weeks|month|months|year|years|m|h|d|w|mo|y"
    r"|jam|menit|detik|hari|minggu|mgu|bulan|tahun)"
    r"(?:\s*\.)?\s+(ago|lalu)",
    re.IGNORECASE,
)


def normalize_youtube_published(text: str, *, warn: bool = True) -> str:
    value = clean_line(text).replace("•", "").strip()
    if not value:
        return ""
    if parse_published_datetime(value):
        return value

    stripped = re.sub(r"^(streamed|premiered|updated|started\s+streaming)\s+", "", value, flags=re.IGNORECASE).strip()
    if stripped != value:
        if parse_published_datetime(stripped):
            return stripped
        english = normalize_english_date(stripped)
        if english:
            return english

    english = normalize_english_date(value)
    if english:
        return english

    lowered = stripped.lower()

    if re.search(r"\bjust\s+now\b|\bmoments?\s+ago\b|\bbaru\s+saja\b|\bbeberapa\s+saat\s+lalu\b", lowered, re.IGNORECASE):
        return now_text()

    match = _TIME_UNITS_AGO.search(lowered)
    if not match:
        if warn:
            logger.warning("unrecognized youtube published text: %r", text[:120])
        return ""

    amount = int(match.group(1))
    unit = match.group(2).rstrip(".").lower()
    now = datetime.now()

    if unit in ("second", "sec", "secs", "detik"):
        target = now - timedelta(seconds=amount)
    elif unit in ("minute", "minutes", "min", "mins", "m", "menit"):
        target = now - timedelta(minutes=amount)
    elif unit in ("hour", "hours", "hr", "hrs", "h", "jam"):
        target = now - timedelta(hours=amount)
    elif unit in ("day", "days", "d", "hari"):
        target = now - timedelta(days=amount)
    elif unit in ("week", "weeks", "w", "minggu", "mgu"):
        target = now - timedelta(weeks=amount)
    elif unit in ("month", "months", "mo", "bulan"):
        target = now - timedelta(days=amount * 30)
    else:
        target = now - timedelta(days=amount * 365)
    return target.strftime("%Y/%m/%d %H:%M")


def extract_video_id_from_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    query_id = parse_qs(parsed.query).get("v", [""])[0].strip()
    if query_id:
        return query_id
    match = re.search(r"/watch\?v=([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    match = re.search(r"/shorts/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    return ""


def _walk_renderer_items(node):
    if isinstance(node, dict):
        for key in ("gridVideoRenderer", "videoRenderer", "reelItemRenderer", "lockupViewModel"):
            if key in node:
                yield node[key]
        if "richItemRenderer" in node:
            yield from _walk_renderer_items(node["richItemRenderer"])
        if "content" in node:
            yield from _walk_renderer_items(node["content"])
        for value in node.values():
            yield from _walk_renderer_items(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_renderer_items(item)


# 频道页链接只认这三种路径（/c/ 旧式短链与外部链接不当作频道）。
_CHANNEL_HREF_PREFIXES = ("/@", "/channel/", "/user/")


def _has_channel_browse(node) -> bool:
    """递归找 browseEndpoint.canonicalBaseUrl 是否指向频道页。"""
    if isinstance(node, dict):
        endpoint = node.get("browseEndpoint")
        if isinstance(endpoint, dict):
            url = str(endpoint.get("canonicalBaseUrl") or "")
            if url.startswith(_CHANNEL_HREF_PREFIXES):
                return True
        return any(_has_channel_browse(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_channel_browse(item) for item in node)
    return False


def _channel_from_lockup(lockup: dict) -> str:
    """lockupViewModel 的频道名藏在 metadataRows 里带频道 browseEndpoint 的 part 上。"""
    meta = (lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    rows = ((meta.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for part in row.get("metadataParts") or []:
            if not isinstance(part, dict):
                continue
            if not _has_channel_browse(part):
                continue
            text = clean_line(str((part.get("text") or {}).get("content") or ""))
            if text:
                return text
    return ""


def _channel_name_from_renderer(renderer: dict) -> str:
    """按优先级取作者名；取不到返回空串（调用方再回落 source["name"]）。"""
    if isinstance((renderer.get("metadata") or {}).get("lockupMetadataViewModel"), dict):
        return _channel_from_lockup(renderer)
    for key in ("ownerText", "shortBylineText", "longBylineText"):
        runs = (renderer.get(key) or {}).get("runs") or []
        for run in runs:
            if not isinstance(run, dict):
                continue
            text = clean_line(str(run.get("text") or ""))
            if text:
                return text
    return ""


def _extract_published_text(renderer: dict) -> str:
    ptt = renderer.get("publishedTimeText")
    if isinstance(ptt, str):
        logger.debug("publishedTimeText is plain string: %r", ptt[:100])
        return ptt.strip()
    if not isinstance(ptt, dict):
        logger.debug("publishedTimeText type unexpected: %s", type(ptt).__name__)
        return ""
    simple = ptt.get("simpleText")
    if isinstance(simple, str) and simple.strip():
        return simple.strip()
    runs = ptt.get("runs")
    if isinstance(runs, list):
        text = " ".join(str(part.get("text") or "") for part in runs)
        if text.strip():
            return text.strip()
    a11y = ptt.get("accessibility", {})
    if isinstance(a11y, dict):
        label = a11y.get("accessibilityData", {}).get("label", "")
        if isinstance(label, str) and label.strip():
            return label.strip()
    logger.debug("publishedTimeText has no usable text field: %s", json.dumps(ptt, ensure_ascii=False)[:200])
    return ""


def _entry_from_lockup(source: dict, lockup: dict, author: str = "") -> FeedEntry | None:
    """解析 lockupViewModel（2026-11 起 YouTube 频道页视频网格的主力结构）。

    contentId 即 videoId；标题在 metadata.lockupMetadataViewModel.title.content；
    发布时间与播放量合并进 contentMetadataViewModel.metadataRows 的 text.content
    （如 "13K"、"11h ago"），只能按文本形态区分，故逐条试 normalize。
    """
    content_type = str(lockup.get("contentType") or "")
    if content_type and content_type != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None
    video_id = str(lockup.get("contentId") or "").strip()
    meta = ((lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {})
    title = clean_line(str((meta.get("title") or {}).get("content") or ""))
    if not video_id or not title:
        return None
    rows = ((meta.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or []
    parts = [
        str((part.get("text") or {}).get("content") or "").strip()
        for row in rows
        if isinstance(row, dict)
        for part in (row.get("metadataParts") or [])
        if isinstance(part, dict)
    ]
    parts = [part for part in parts if part]
    published_text = next((part for part in parts if normalize_youtube_published(part, warn=False)), "")
    view_text = " ".join(part for part in parts if part != published_text)
    return FeedEntry(
        source_id=source["id"],
        source_name=author or source["name"],
        title=title,
        link=f"https://www.youtube.com/watch?v={video_id}",
        published=normalize_youtube_published(published_text) or now_text(),
        summary=clean_line(" ".join(part for part in (published_text, view_text) if part)),
    )


def _entry_from_renderer(source: dict, renderer: dict, author: str = "") -> FeedEntry | None:
    if isinstance((renderer.get("metadata") or {}).get("lockupMetadataViewModel"), dict):
        return _entry_from_lockup(source, renderer, author)
    video_id = str(renderer.get("videoId") or "").strip()
    title_runs = ((renderer.get("title") or {}).get("runs") or [])
    title = clean_line(" ".join(str(part.get("text") or "") for part in title_runs))
    if not video_id or not title:
        return None
    published_text = _extract_published_text(renderer)
    view_text = (
        ((renderer.get("viewCountText") or {}).get("simpleText"))
        or " ".join(str(part.get("text") or "") for part in ((renderer.get("viewCountText") or {}).get("runs") or []))
    ).strip()
    normalized_published = normalize_youtube_published(published_text)
    if not normalized_published and view_text:
        normalized_published = normalize_youtube_published(view_text)
    return FeedEntry(
        source_id=source["id"],
        source_name=author or source["name"],
        title=title,
        link=f"https://www.youtube.com/watch?v={video_id}",
        published=normalized_published or now_text(),
        summary=clean_line(" ".join(part for part in (published_text, view_text) if part)),
    )


def _read_initial_data(page) -> dict | None:
    """从整页 HTML 取 ytInitialData；缺失或 JSON 坏掉返回 None。"""
    html = page.content()
    match = re.search(r"var ytInitialData = (\{.*?\});", html, flags=re.S)
    if not match:
        match = re.search(r"window\[['\"]ytInitialData['\"]\]\s*=\s*(\{.*?\});", html, flags=re.S)
    if not match:
        logger.debug("ytInitialData regex not found in page (len=%d)", len(html))
        return None
    try:
        payload = json.loads(match.group(1))
    except Exception as exc:
        logger.debug("ytInitialData JSON parse failed: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


def extract_youtube_entries_from_initial_data(page, source: dict, limit: int = 12) -> list[FeedEntry]:
    payload = _read_initial_data(page)
    if payload is None:
        return []

    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    for renderer in _walk_renderer_items(payload):
        try:
            entry = _entry_from_renderer(source, renderer)
        except Exception:
            continue
        if not entry or entry.link in seen_links:
            continue
        seen_links.add(entry.link)
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def extract_youtube_entries_from_lockups(page, source: dict, limit: int = 12) -> list[FeedEntry]:
    rows = page.locator("ytd-rich-item-renderer, ytd-grid-video-renderer, ytd-video-renderer").evaluate_all(
        """(els, limit) => {
          const items = [];
          for (const el of els) {
            if (items.length >= limit) break;
            const root = el.querySelector('.ytLockupViewModelHost') || el;
            const imageLink = root.querySelector('a.ytLockupViewModelContentImage[href*="/watch"]');
            const href = imageLink?.getAttribute('href') || '';
            const classes = root.className || '';
            const contentIdMatch = classes.match(/content-id-([A-Za-z0-9_-]+)/);
            const rawText = (el.innerText || '').trim();
            const lines = rawText.split('\\n').map(x => x.trim()).filter(Boolean);
            items.push({
              href,
              contentId: contentIdMatch ? contentIdMatch[1] : '',
              lines,
              text: rawText,
            });
          }
          return items;
        }""",
        limit,
    )

    if not rows:
        logger.debug("lockup: no rows found from DOM selectors")
        return []

    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    for item in rows:
        href = str(item.get("href") or "").strip()
        video_id = extract_video_id_from_url(href) or str(item.get("contentId") or "").strip()
        if not video_id:
            continue
        link = f"https://www.youtube.com/watch?v={video_id}"
        if link in seen_links:
            continue

        lines = [clean_line(line) for line in (item.get("lines") or []) if clean_line(line)]
        if len(lines) < 3:
            continue

        title = lines[1]
        published = ""
        summary_parts: list[str] = []
        for idx, line in enumerate(lines):
            if idx == 1:
                continue
            normalized = normalize_youtube_published(line)
            if normalized and not published:
                published = normalized
            if idx >= 2:
                summary_parts.append(line)

        if not published:
            raw = str(item.get("text") or "")
            published = normalize_youtube_published(raw)

        if not title:
            continue

        seen_links.add(link)
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=published or now_text(),
                summary=clean_line(" ".join(summary_parts[:4])),
            )
        )
        if len(entries) >= limit:
            break
    return entries


def extract_youtube_entries(page, source: dict, limit: int = 12) -> list[FeedEntry]:
    for name, extractor in (("initial_data", extract_youtube_entries_from_initial_data), ("lockups", extract_youtube_entries_from_lockups)):
        entries = extractor(page, source, limit=limit)
        logger.info("youtube extractor '%s' returned %d entries", name, len(entries))
        if entries:
            return entries
    return []


def _apply_youtube_language_pref(page) -> None:
    """尽力把 YouTube 界面语言/地区设为 en/US，缺失写入面时静默跳过。

    PREF cookie 只是让页面按 en/US 渲染，作用是让 publishedTimeText 等文案落到
    提取器已覆盖的英文相对时间（见 normalize_youtube_published）。真 Playwright page
    （测试/离线工具）有 page.context.add_cookies 可写；bsk page facade 无 cookie
    写入命令，但 goto 已带 hl=en&gl=US 查询参数——服务端按查询参数应用语言/地区，
    对本次加载等效，故 bsk 下该 cookie 非必需（提取器解析的是语言无关的 renderer
    JSON 与 DOM 结构，不依赖此 cookie）。
    """
    context = getattr(page, "context", None)
    add_cookies = getattr(context, "add_cookies", None)
    if not callable(add_cookies):
        return
    add_cookies([
        {"name": "PREF", "value": "hl=en&gl=US", "domain": ".youtube.com", "path": "/"},
    ])


def fetch_youtube_with_page(page, source: dict, timeout_ms: int = 60000, limit: int = 12) -> FeedFetchResult:
    limit = max(1, min(limit, 12))
    target = resolve_web_target(source)
    if not target or target.site != "youtube":
        return result_error(source, "暂不支持的 YouTube 网页源")
    try:
        _apply_youtube_language_pref(page)
        page.goto(with_query_params(target.page_url, {"hl": "en", "gl": "US"}), wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(5000)
        try:
            page.locator("ytd-rich-grid-renderer, ytd-section-list-renderer, ytd-rich-item-renderer").first.wait_for(timeout=15000)
        except Exception:
            pass
        entries = extract_youtube_entries(page, source, limit=limit)
    except PlaywrightTimeoutError as exc:
        return result_error(source, f"YouTube 网页直抓超时: {exc}")
    except Exception as exc:
        return result_error(source, f"YouTube 网页直抓失败: {exc}")

    if not entries:
        return result_error(source, "YouTube 页面可访问，但未解析到可入库内容")
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )


# DOM 兜底：从卡片里按 videoId 找频道链接文本。只在 ytInitialData 拿不到作者时跑一次。
_DOM_CHANNEL_MAP_JS = """
(limit) => {
  const out = [];
  const seen = new Set();
  const cards = document.querySelectorAll(
    'ytd-rich-item-renderer, ytd-video-renderer, yt-lockup-view-model'
  );
  for (const card of cards) {
    if (out.length >= limit) break;
    const anchors = card.querySelectorAll('a[href*="/watch"], a[href*="/shorts/"]');
    let videoId = '';
    for (const a of anchors) {
      const href = a.getAttribute('href') || '';
      const m = href.match(/[?&]v=([A-Za-z0-9_-]+)/) || href.match(/\\/shorts\\/([A-Za-z0-9_-]+)/);
      if (m) { videoId = m[1]; break; }
    }
    if (!videoId || seen.has(videoId)) continue;
    const nameEl = card.querySelector(
      'ytd-channel-name a, #channel-name a, #byline-container a, a[href^="/@"], a[href^="/channel/"], a[href^="/user/"]'
    );
    const channelName = nameEl ? (nameEl.textContent || '').trim() : '';
    if (!channelName) continue;
    seen.add(videoId);
    out.push({videoId, channelName});
  }
  return out;
}
"""


def _dom_channel_map(page, video_ids: set[str]) -> dict[str, str]:
    """按 videoId 从 DOM 补频道名；fake page 没有 evaluate_all 时防御性跳过。"""
    evaluate_all = getattr(page, "evaluate_all", None)
    if not callable(evaluate_all):
        return {}
    try:
        rows = evaluate_all(_DOM_CHANNEL_MAP_JS, len(video_ids))
    except Exception as exc:
        logger.debug("youtube subscriptions DOM channel map failed: %s", exc)
        return {}
    mapping: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        video_id = str(row.get("videoId") or "").strip()
        channel = clean_line(str(row.get("channelName") or ""))
        if video_id in video_ids and channel:
            mapping[video_id] = channel
    return mapping


def _fill_missing_authors(page, entries: list[FeedEntry], source: dict) -> None:
    pending = {
        extract_video_id_from_url(entry.link)
        for entry in entries
        if entry.source_name == source["name"]
    }
    pending.discard("")
    if not pending:
        return
    mapping = _dom_channel_map(page, pending)
    if not mapping:
        return
    for entry in entries:
        if entry.source_name != source["name"]:
            continue
        channel = mapping.get(extract_video_id_from_url(entry.link))
        if channel:
            entry.source_name = channel


def _entries_from_subscription_initial_data(
    payload: dict, source: dict, limit: int = 12
) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    for renderer in _walk_renderer_items(payload):
        try:
            author = _channel_name_from_renderer(renderer)
            entry = _entry_from_renderer(source, renderer, author)
        except Exception:
            continue
        if not entry or entry.link in seen_links:
            continue
        seen_links.add(entry.link)
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def fetch_youtube_subscriptions_with_page(
    page, source: dict, timeout_ms: int = 60000, limit: int = 12
) -> FeedFetchResult:
    """YouTube 订阅流（我关注的全部频道）：只解析 ytInitialData，作者写进 entry.source_name。

    ponytail: v1 只吃首屏（上限 30 条），不做 continuation 翻页——订阅页首屏足够日常增量；
    天花板是「首屏之后的新内容抓不到」。升级路径：按 ytInitialData 里的
    continuation token 调 youtubei/v1/browse 拉后续页。
    """
    limit = max(1, min(limit, 30))
    target = resolve_web_target(source)
    if not target or target.site != "youtube_subscriptions":
        return result_error(source, "暂不支持的 YouTube 订阅流源")
    try:
        _apply_youtube_language_pref(page)
        page.goto(
            with_query_params(target.page_url, {"hl": "en", "gl": "US"}),
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        page.wait_for_timeout(5000)
        try:
            page.locator(
                "ytd-rich-grid-renderer, ytd-rich-item-renderer, ytd-video-renderer"
            ).first.wait_for(timeout=15000)
        except Exception:
            pass

        current_url = str(getattr(page, "url", "") or "")
        if "accounts.google.com" in current_url:
            return result_error(
                source,
                "YouTube 订阅流需要登录态：页面跳转到 accounts.google.com，请在日常 Chrome 登录 YouTube 后重试",
            )

        payload = _read_initial_data(page)
        if payload is None:
            return result_error(
                source,
                "YouTube 订阅流需要登录态：未获取到 ytInitialData，请在日常 Chrome 登录 YouTube 后重试",
            )
        if not any(True for _ in _walk_renderer_items(payload)):
            return result_error(
                source,
                "YouTube 订阅流需要登录态：订阅页未返回任何视频项，请在日常 Chrome 登录 YouTube 后重试",
            )

        entries = _entries_from_subscription_initial_data(payload, source, limit=limit)
        _fill_missing_authors(page, entries, source)
    except PlaywrightTimeoutError as exc:
        return result_error(source, f"YouTube 订阅流抓取超时: {exc}")
    except Exception as exc:
        return result_error(source, f"YouTube 订阅流抓取失败: {exc}")

    if not entries:
        return result_error(source, "YouTube 订阅页面可访问，但未解析到可入库内容")
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )

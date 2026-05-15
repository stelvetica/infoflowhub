from __future__ import annotations

import re
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import (
    DOUYIN_LOGIN_HINT,
    clean_line,
    fallback_published,
    now_text,
    parse_published_datetime,
    resolve_web_target,
    result_error,
)

DOUYIN_LOGIN_WALL_MARKERS = (
    "登录后查看更多",
    "打开抖音 App",
    "扫码登录",
    "验证码登录",
    "登录后即可查看",
    "请完成验证",
)
DOUYIN_RISK_MARKERS = (
    "访问受限",
    "网络不给力",
    "内容暂时无法查看",
    "验证后继续访问",
)
DOUYIN_TIME_PATTERNS = [
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
]


def _normalize_douyin_link(url: str) -> str:
    value = (url or "").strip().split("?", 1)[0]
    match = re.search(r"/video/(\d+)", value)
    if not match:
        return ""
    return f"https://www.douyin.com/video/{match.group(1)}"


def _normalize_douyin_published(text: str) -> str:
    value = clean_line(text).replace("发布于", "").replace("发表于", "").strip()
    if not value:
        return ""
    for pattern in DOUYIN_TIME_PATTERNS:
        try:
            parsed = datetime.strptime(value, pattern)
            if pattern in ("%Y-%m-%d", "%Y/%m/%d"):
                return parsed.strftime("%Y/%m/%d 00:00")
            return parsed.strftime("%Y/%m/%d %H:%M")
        except ValueError:
            continue
    return value if parse_published_datetime(value) else ""


def _normalize_douyin_timestamp(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        try:
            ts = int(raw)
            if ts > 10**12:
                ts //= 1000
            if ts > 10**9:
                return datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M")
        except Exception:
            return ""
    return _normalize_douyin_published(raw)


def _extract_payload_maps(payload: dict) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    publish_map: dict[str, str] = {}
    title_map: dict[str, str] = {}
    summary_map: dict[str, str] = {}
    aweme_list = payload.get("aweme_list") or []
    if not isinstance(aweme_list, list):
        return publish_map, title_map, summary_map
    for item in aweme_list:
        if not isinstance(item, dict):
            continue
        aweme_id = str(item.get("aweme_id") or item.get("awemeId") or "").strip()
        if not aweme_id:
            continue
        published = _normalize_douyin_timestamp(
            item.get("create_time") or item.get("createTime") or item.get("publish_time") or item.get("publishTime")
        )
        if published:
            publish_map[aweme_id] = published
        raw_title = clean_line(
            str(
                item.get("desc")
                or item.get("title")
                or ((item.get("share_info") or {}).get("share_title"))
                or ((item.get("seo_info") or {}).get("seo_title"))
                or ""
            )
        )
        if raw_title:
            title_map[aweme_id] = raw_title
        raw_summary = clean_line(
            str(
                item.get("desc")
                or item.get("raw_text")
                or ((item.get("share_info") or {}).get("share_desc"))
                or raw_title
            )
        )
        if raw_summary:
            summary_map[aweme_id] = raw_summary
    return publish_map, title_map, summary_map


def _collect_payload_maps_from_network(
    page, target_url: str, timeout_ms: int
) -> tuple[dict[str, str], dict[str, str], dict[str, str], str]:
    publish_map: dict[str, str] = {}
    title_map: dict[str, str] = {}
    summary_map: dict[str, str] = {}
    response_error = ""

    def on_response(response):
        nonlocal response_error
        try:
            url = response.url
            if "/aweme/v1/web/aweme/post/" not in url:
                return
            payload = response.json()
            payload_publish_map, payload_title_map, payload_summary_map = _extract_payload_maps(payload)
            publish_map.update(payload_publish_map)
            title_map.update(payload_title_map)
            summary_map.update(payload_summary_map)
        except Exception as exc:
            response_error = str(exc)

    page.on("response", on_response)
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(7000)
    finally:
        page.remove_listener("response", on_response)
    return publish_map, title_map, summary_map, response_error


def _extract_douyin_cards(page, limit: int = 12) -> list[dict]:
    js = r"""
    (limit) => {
      const results = [];
      const seen = new Set();
      const anchors = Array.from(document.querySelectorAll('a[href*="/video/"]'));
      for (const a of anchors) {
        if (results.length >= limit * 4) break;
        const href = (a.href || '').split('?')[0];
        const match = href.match(/\/video\/(\d+)/);
        if (!match) continue;
        const videoId = match[1];
        if (seen.has(videoId)) continue;
        seen.add(videoId);
        const card =
          a.closest('li') ||
          a.closest('[data-e2e*="user-post-list"] > div') ||
          a.closest('[class*="video"]') ||
          a.closest('[class*="item"]') ||
          a.parentElement;
        const text = card ? (card.innerText || card.textContent || '').replace(/\s+/g, ' ').trim() : '';
        let title = '';
        const titleCandidates = card
          ? Array.from(card.querySelectorAll('[data-e2e*="desc"], [data-e2e*="video-desc"], [class*="desc"], img[alt]'))
          : [];
        for (const node of titleCandidates) {
          const candidate = node.tagName === 'IMG' ? (node.getAttribute('alt') || '') : (node.innerText || node.textContent || '');
          const cleaned = candidate.replace(/\s+/g, ' ').trim();
          if (cleaned && !/^[\d.万亿wW\s]+$/.test(cleaned)) {
            title = cleaned;
            break;
          }
        }
        const timeCandidates = card
          ? Array.from(card.querySelectorAll('time, [data-e2e*="publish"], [class*="time"], [class*="date"]'))
          : [];
        let published = '';
        for (const node of timeCandidates) {
          const candidate = (node.getAttribute('datetime') || node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
          if (candidate) {
            published = candidate;
            break;
          }
        }
        results.push({
          video_id: videoId,
          href,
          title,
          published,
          raw_text: text,
        });
      }
      return results;
    }
    """
    return page.evaluate(js, limit)


def _normalize_card_to_entry(
    source: dict,
    item: dict,
    publish_map: dict[str, str],
    title_map: dict[str, str],
    summary_map: dict[str, str],
) -> FeedEntry | None:
    link = _normalize_douyin_link(str(item.get("href") or ""))
    if not link:
        return None
    video_id = str(item.get("video_id") or "").strip()
    title = clean_line(str(item.get("title") or ""))
    raw_text = clean_line(str(item.get("raw_text") or ""))
    payload_title = clean_line(title_map.get(video_id, ""))
    payload_summary = clean_line(summary_map.get(video_id, ""))
    if not title:
        title = payload_title
    if not title:
        title = f"抖音视频 {video_id}" if video_id else "抖音视频"
    published = publish_map.get(video_id) or _normalize_douyin_published(str(item.get("published") or ""))
    return FeedEntry(
        source_id=source["id"],
        source_name=source["name"],
        title=title,
        link=link,
        published=fallback_published(published or now_text()),
        summary=raw_text or payload_summary or title,
    )


def fetch_douyin_subscription_with_page(page, source: dict, timeout_ms: int = 60000, limit: int = 12) -> FeedFetchResult:
    limit = max(1, min(limit, 12))
    target = resolve_web_target(source)
    if not target or target.site != "douyin":
        return result_error(source, "暂不支持的抖音订阅源")

    try:
        publish_map, title_map, summary_map, response_error = _collect_payload_maps_from_network(
            page, target.page_url, timeout_ms
        )
        try:
            page.locator('a[href*="/video/"]').first.wait_for(timeout=12000)
        except Exception:
            pass
        body_text = clean_line(page.locator("body").inner_text())
        cards = _extract_douyin_cards(page, limit=limit)
    except PlaywrightTimeoutError as exc:
        return result_error(source, f"抖音订阅抓取超时: {exc}")
    except Exception as exc:
        return result_error(source, f"抖音订阅抓取失败: {exc}")

    if any(marker in body_text for marker in DOUYIN_LOGIN_WALL_MARKERS):
        return result_error(source, f"抖音登录态失效或未登录。{DOUYIN_LOGIN_HINT}")
    if any(marker in body_text for marker in DOUYIN_RISK_MARKERS):
        return result_error(source, "抖音页面触发访问限制或风控，请稍后重试并确认当前登录态可正常访问该主页")
    if response_error and not publish_map and not title_map:
        return result_error(source, f"抖音订阅时间解析失败: {response_error}")

    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    for card in cards:
        entry = _normalize_card_to_entry(source, card, publish_map, title_map, summary_map)
        if not entry or entry.link in seen_links:
            continue
        seen_links.add(entry.link)
        entries.append(entry)
        if len(entries) >= limit:
            break

    entries.sort(key=lambda item: parse_published_datetime(item.published) or datetime.min, reverse=True)
    if not entries:
        return result_error(source, "抖音主页可访问，但当前未解析到可入库视频内容")

    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=True,
        status=200,
        entries=entries,
        error="",
    )

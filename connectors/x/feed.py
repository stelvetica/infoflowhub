from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import (
    X_LOGIN_HINT,
    clean_line,
    fallback_published,
    is_macromargin_source,
    normalize_english_date,
    parse_published_datetime,
    resolve_web_target,
    result_error,
)

X_ANTI_DETECT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
// Override permissions query to hide automation
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) => (
  params.name === 'notifications' ? Promise.resolve({ state: 'denied' }) : originalQuery(params)
);
"""

DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "runtime" / "debug"


def parse_x_posts(body_text: str, username: str, limit: int = 8) -> list[FeedEntry]:
    lines = [clean_line(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]
    entries: list[FeedEntry] = []
    seen_titles: set[str] = set()
    header_line = f"@{username}"
    i = 0
    while i < len(lines):
        if lines[i] != header_line:
            i += 1
            continue
        if i == 0 or i + 2 >= len(lines):
            i += 1
            continue
        name_line = lines[i - 1]
        if not name_line or "posts" in name_line.lower():
            i += 1
            continue
        date_index = -1
        for j in range(i + 1, min(i + 8, len(lines))):
            if normalize_english_date(lines[j]):
                date_index = j
                break
        if date_index < 0:
            i += 1
            continue
        content_parts: list[str] = []
        j = date_index + 1
        while j < len(lines):
            line = lines[j]
            if line == header_line:
                break
            if line in {"Show more", "Show translation", "Follow", "Posts", "Replies", "Media"}:
                j += 1
                continue
            if line.startswith("http://") or line.startswith("https://"):
                content_parts.append(line)
                j += 1
                continue
            if re.fullmatch(r"[\d,.]+[KM]?", line):
                break
            if line in {"Log in", "Sign up", "Create account", "New to X?"}:
                if content_parts:
                    break
                j += 1
                continue
            content_parts.append(line)
            j += 1
        title = clean_line(" ".join(content_parts[:2]))
        summary = clean_line(" ".join(content_parts[:6]))
        if title and title not in seen_titles:
            seen_titles.add(title)
            entries.append(
                FeedEntry(
                    source_id="",
                    source_name="",
                    title=title,
                    link=f"https://x.com/{username}",
                    published=normalize_english_date(lines[date_index]),
                    summary=summary,
                )
            )
            if len(entries) >= limit:
                break
        i = j
    return entries


def extract_x_status_map(page, username: str) -> list[dict]:
    items = page.locator(f'a[href*="/{username}/status/"]').evaluate_all(
        """els => {
          const seen = new Set();
          const result = [];
          for (const el of els) {
            const href = el.href || '';
            const match = href.match(/\\/status\\/(\\d+)/);
            if (!match) continue;
            if (href.includes('/analytics') || href.includes('/photo/')) continue;
            const statusId = match[1];
            if (seen.has(statusId)) continue;
            seen.add(statusId);
            result.push({
              status_id: statusId,
              href,
              date_text: (el.innerText || el.textContent || '').trim(),
            });
          }
          return result;
        }"""
    )
    return [item for item in items if item.get("href")]


def extract_x_articles(page, username: str, limit: int = 8) -> list[dict]:
    items = page.locator("article").evaluate_all(
        """(els, params) => {
          const { username, limit } = params;
          const result = [];
          const seen = new Set();
          for (const article of els) {
            if (result.length >= limit) break;
            const statusLink = article.querySelector(`a[href*="/${username}/status/"]`);
            if (!statusLink) continue;
            const href = statusLink.href || '';
            const match = href.match(/\\/status\\/(\\d+)/);
            if (!match) continue;
            if (href.includes('/analytics') || href.includes('/photo/')) continue;
            const statusId = match[1];
            if (seen.has(statusId)) continue;
            seen.add(statusId);
            const timeEl = article.querySelector('time');
            const textNodes = Array.from(article.querySelectorAll('[data-testid="tweetText"]'));
            result.push({
              status_id: statusId,
              href,
              published: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
              text: textNodes
                .map(node => (node.innerText || node.textContent || '').trim())
                .filter(Boolean)
                .join('\\n'),
              article_text: (article.innerText || article.textContent || '').trim(),
            });
          }
          return result;
        }""",
        {"username": username, "limit": limit},
    )
    return [item for item in items if item.get("href")]


def collect_x_articles(page, username: str, limit: int = 12, rounds: int = 4) -> list[dict]:
    merged: dict[str, dict] = {}
    previous_count = -1
    for _ in range(rounds):
        items = extract_x_articles(page, username, limit=max(limit * 3, 24))
        for item in items:
            href = (item.get("href") or "").strip()
            if not href:
                continue
            merged[href] = item
        if len(merged) == previous_count:
            break
        previous_count = len(merged)
        page.mouse.wheel(0, 2600)
        page.wait_for_timeout(1800)
    return list(merged.values())


def normalize_iso_datetime(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y/%m/%d %H:%M")
    except ValueError:
        return ""


def parse_x_article_text(raw_text: str) -> tuple[str, str]:
    lines = [clean_line(line) for line in (raw_text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "", ""

    skip_exact = {
        "Show more",
        "Show translation",
        "Translate post",
        "Translate bio",
        "Follow",
        "Following",
        "Verified account",
        "Pinned",
        "Pinned post",
        "Post",
        "Replying to",
        "Article",
        "\u6587\u7ae0",
        "\u00b7",
        "\u2022",
        "\u2026",
        "...",
    }
    time_patterns = (
        r"\d+[smhdwy]$",
        r"\d+\s*(?:\u79d2|\u5206\u949f|\u5c0f\u65f6|\u5929|\u5468|\u6708|\u5e74)\u524d",
        r"\d{1,2}\u6708\d{1,2}\u65e5",
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(,\s+\d{4})?$",
    )
    filtered: list[str] = []
    for line in lines:
        if line in skip_exact:
            continue
        if line.startswith("@"):
            continue
        if re.fullmatch(r"[\d,.]+[KMB]?", line):
            continue
        if any(re.fullmatch(pattern, line, flags=re.IGNORECASE) for pattern in time_patterns):
            continue
        if re.search(r"(?:\u5173\u6ce8\u4e2d|\u6b63\u5728\u5173\u6ce8|Following)$", line):
            continue
        filtered.append(line)

    article_title = ""
    joined = clean_line(" ".join(lines))
    match = re.search(
        r"\u6587\u7ae0\s+(.+?)(?=\s+(?:\u672c\u5468|\u4eca\u65e5|\u4eca\u5929|\u6628\u65e5|\u539f\u6587|\u5168\u6587|\u9605\u8bfb|http|https)|$)",
        joined,
    )
    if match:
        article_title = clean_line(match.group(1))
    if article_title and article_title not in skip_exact:
        filtered = [article_title, *[line for line in filtered if line != article_title]]

    if not filtered:
        return "", ""
    return filtered[0], " ".join(filtered[:6]).strip()


def _wait_for_tweets(page, timeout_ms: int = 30000) -> bool:
    try:
        page.wait_for_selector('[data-testid="tweetText"]', timeout=timeout_ms)
        return True
    except (PlaywrightTimeoutError, Exception):
        return False


def _save_debug_assets(page, source: dict, suffix: str = ""):
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = (source.get("id") or "x_debug").replace("/", "_")
    label = f"{safe_name}_{suffix}_{ts}" if suffix else f"{safe_name}_{ts}"
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
    except Exception:
        pass
    try:
        text = page.locator("body").inner_text(timeout=5000)
        (DEBUG_DIR / f"{label}.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass
    try:
        html = page.content()
        (DEBUG_DIR / f"{label}.html").write_text(html[0:50000], encoding="utf-8")
    except Exception:
        pass


def fetch_x_with_page(page, source: dict, timeout_ms: int = 60000, limit: int = 12) -> FeedFetchResult:
    # X 不是普通 RSS 直连源。这里统一复用 x_profile2 这份共享真人登录态。
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的 X 页面目标")

    # 反自动化检测
    page.add_init_script(X_ANTI_DETECT_SCRIPT)

    try:
        page.goto(target.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(5000)
        tweets_visible = _wait_for_tweets(page, timeout_ms=15000)
        if not tweets_visible:
            _save_debug_assets(page, source, "no_tweets")
        page.wait_for_timeout(2000)
        body_text = page.locator("body").inner_text()
    except PlaywrightTimeoutError as exc:
        if is_macromargin_source(source):
            return result_error(source, f"MacroMargin 抓取超时。{X_LOGIN_HINT}")
        return result_error(source, f"X 网页直抓超时: {exc}")
    except Exception as exc:
        _save_debug_assets(page, source, "error")
        if is_macromargin_source(source):
            return result_error(source, f"MacroMargin 抓取失败：{exc}。{X_LOGIN_HINT}")
        return result_error(source, f"X 网页直抓失败: {exc}")

    article_items = collect_x_articles(page, target.uid, limit=12, rounds=5)
    status_items = extract_x_status_map(page, target.uid)
    entries = parse_x_posts(body_text, target.uid, limit=max(len(status_items), 8))
    normalized_entries: list[FeedEntry] = []
    seen_links: set[str] = set()

    for item in article_items:
        link = (item.get("href") or "").strip()
        title, summary = parse_x_article_text(item.get("text") or item.get("article_text", ""))
        if not title or not link or link in seen_links:
            continue
        seen_links.add(link)
        normalized_entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=fallback_published(normalize_iso_datetime(item.get("published", ""))),
                summary=summary,
            )
        )

    for index, item in enumerate(entries):
        link = item.link
        if index < len(status_items):
            link = status_items[index].get("href") or link
            if not item.published:
                item.published = normalize_english_date(status_items[index].get("date_text", ""))
        if not item.title or not link or link in seen_links:
            continue
        seen_links.add(link)
        normalized_entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=item.title,
                link=link,
                published=fallback_published(item.published),
                summary=item.summary,
            )
        )

    normalized_entries.sort(
        key=lambda item: parse_published_datetime(item.published) or datetime.min,
        reverse=True,
    )
    normalized_entries = normalized_entries[:12]
    if not normalized_entries and is_macromargin_source(source):
        _save_debug_assets(page, source, "empty")
        return result_error(source, f"MacroMargin 页面已打开，但未解析到可入库内容。{X_LOGIN_HINT}")

    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=bool(normalized_entries),
        status=200 if normalized_entries else 0,
        entries=normalized_entries,
        error="" if normalized_entries else "X 页面可访问，但未解析到可入库内容",
    )

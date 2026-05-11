from __future__ import annotations

import re
from datetime import datetime, timedelta

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors.web.common import clean_line, fallback_published, normalize_relative_date, resolve_web_target, result_error


def extract_weibo_cards_from_dom(page, limit: int = 8) -> list[dict]:
    selectors = [
        '[action-type="feed_list_item"]',
        '[mid]',
        "article",
    ]
    for selector in selectors:
        cards = page.locator(selector).evaluate_all(
            f"""els => els.slice(0, {limit * 4}).map(el => {{
                const text = (el.innerText || el.textContent || '').trim();
                const link = el.querySelector('a[href*="/status/"], a[href*="/detail/"]');
                return {{
                    text,
                    href: link ? link.href || '' : '',
                    mid: el.getAttribute('mid') || el.getAttribute('data-mid') || ''
                }};
            }})"""
        )
        cards = [item for item in cards if item.get("text")]
        if cards:
            return cards
    return []


def is_weibo_date_line(line: str) -> bool:
    text = clean_line(line)
    return bool(
        re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text)
        or re.fullmatch(r"\d{1,2}-\d{1,2}", text)
        or re.fullmatch(r"\d+\s*分钟前", text)
        or re.fullmatch(r"\d+\s*小时前", text)
        or re.fullmatch(r"今天\s+\d{2}:\d{2}", text)
        or re.fullmatch(r"昨天\s+\d{2}:\d{2}", text)
    )


def normalize_weibo_date(line: str) -> str:
    text = clean_line(line)
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
        dt = datetime.strptime(text, "%Y-%m-%d")
        return dt.strftime("%Y/%m/%d 00:00")
    if re.fullmatch(r"\d{1,2}-\d{1,2}", text):
        dt = datetime.strptime(f"{datetime.now().year}-{text}", "%Y-%m-%d")
        return dt.strftime("%Y/%m/%d 00:00")
    if re.fullmatch(r"今天\s+\d{2}:\d{2}", text):
        hm = text.split()[1]
        return datetime.now().strftime(f"%Y/%m/%d {hm}")
    if re.fullmatch(r"昨天\s+\d{2}:\d{2}", text):
        hm = text.split()[1]
        return (datetime.now() - timedelta(days=1)).strftime(f"%Y/%m/%d {hm}")
    return normalize_relative_date(text)


def parse_weibo_posts(body_text: str, source: dict, uid: str, limit: int = 8) -> list[FeedEntry]:
    lines = [clean_line(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]
    entries: list[FeedEntry] = []
    seen_titles: set[str] = set()
    stop_lines = {
        "关注推荐",
        "帮助中心",
        "微博客服 4000-960-960",
        "自助服务中心",
        "常见问题",
        "合作&服务",
        "更多",
        "关于微博",
        "About Weibo",
        "客户端下载",
        "微博招聘",
        "网站备案信息",
        "微博隐私安全中心",
    }
    i = 0
    while i < len(lines):
        if "全部微博" not in lines[i] and i > 0 and not is_weibo_date_line(lines[i + 1] if i + 1 < len(lines) else ""):
            i += 1
            continue
        start = i + 1 if "全部微博" in lines[i] else i
        if start + 2 >= len(lines):
            i += 1
            continue
        author_line = lines[start]
        date_index = -1
        for j in range(start + 1, min(start + 6, len(lines))):
            if is_weibo_date_line(lines[j]):
                date_index = j
                break
        if date_index < 0:
            i += 1
            continue
        content_parts: list[str] = []
        j = date_index + 1
        while j < len(lines):
            line = lines[j]
            if line == author_line and j + 1 < len(lines) and is_weibo_date_line(lines[j + 1]):
                break
            if line in stop_lines:
                break
            if line in {
                "来自 iPhone 14 Pro Max",
                "来自 微博网页版",
                "来自 iPhone",
                "来自 Android",
                "置顶",
                "精选",
                "微博",
                "视频",
                "相册",
                "文章",
                "全文",
                "...展开",
                "关注推荐",
            }:
                j += 1
                continue
            if re.fullmatch(r"[\d.]+[万亿]?", line):
                if content_parts:
                    break
                j += 1
                continue
            if "博主设置仅对粉丝展示全部微博内容" in line:
                break
            content_parts.append(line)
            j += 1
        title = clean_line(" ".join(content_parts[:2]))
        summary = clean_line(" ".join(content_parts[:6]))
        if title and title not in seen_titles:
            if "置顶" in title or title.startswith("置顶帖"):
                i = j
                continue
            seen_titles.add(title)
            published = fallback_published(normalize_weibo_date(lines[date_index]))
            link_slug = published.replace("/", "").replace(" ", "").replace(":", "")
            entries.append(
                FeedEntry(
                    source_id=source["id"],
                    source_name=source["name"],
                    title=title,
                    link=f"https://weibo.com/{uid}/#post-{link_slug}",
                    published=published,
                    summary=summary,
                )
            )
        if len(entries) >= limit:
            break
        i = j
    return entries


def parse_weibo_posts_from_cards(cards: list[dict], source: dict, uid: str, limit: int = 8) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    seen_titles: set[str] = set()
    for card in cards:
        lines = [clean_line(line) for line in (card.get("text") or "").splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            continue
        published = ""
        content_parts: list[str] = []
        for line in lines:
            if not published and is_weibo_date_line(line):
                published = normalize_weibo_date(line)
                continue
            if line in {"微博", "全文", "...展开", "置顶", "精选", "视频", "相册", "文章"}:
                continue
            if line.startswith("来自 "):
                continue
            if re.fullmatch(r"[\d.]+[万亿]?", line):
                continue
            content_parts.append(line)
        title = clean_line(" ".join(content_parts[:2]))
        summary = clean_line(" ".join(content_parts[:6]))
        if not title or title in seen_titles:
            continue
        if "置顶" in title or title.startswith("置顶帖") or any("置顶" in line for line in lines[:4]):
            continue
        seen_titles.add(title)
        link = (card.get("href") or "").strip()
        if not link:
            link_slug = (card.get("mid") or fallback_published(published)).replace("/", "").replace(" ", "").replace(":", "")
            link = f"https://weibo.com/{uid}/#post-{link_slug}"
        entries.append(
            FeedEntry(
                source_id=source["id"],
                source_name=source["name"],
                title=title,
                link=link,
                published=fallback_published(published),
                summary=summary,
            )
        )
        if len(entries) >= limit:
            break
    return entries


def fetch_weibo_with_page(page, source: dict, timeout_ms: int = 60000) -> FeedFetchResult:
    target = resolve_web_target(source)
    if not target:
        return result_error(source, "暂不支持的网页直抓站点")
    try:
        page.goto(target.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(7000)
        body_text = page.locator("body").inner_text()
        dom_cards = extract_weibo_cards_from_dom(page)
    except PlaywrightTimeoutError as exc:
        return result_error(source, f"微博网页直抓超时: {exc}")
    except Exception as exc:
        return result_error(source, f"微博网页直抓失败: {exc}")

    if any(flag in body_text for flag in ["登录/注册", "请登录后使用", "还没有微博？立即注册", "扫码登录更安全"]):
        return result_error(source, "微博公开页被登录墙拦截，当前需要登录态")
    entries = parse_weibo_posts_from_cards(dom_cards, source, target.uid) if dom_cards else []
    if not entries:
        entries = parse_weibo_posts(body_text, source, target.uid)
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=target.page_url,
        ok=bool(entries),
        status=200 if entries else 0,
        entries=entries,
        error="" if entries else "微博页面可访问，但当前未解析到可入库内容",
    )

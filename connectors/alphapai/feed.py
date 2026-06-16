from __future__ import annotations

import hashlib
import re
import sqlite3
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from bs4 import BeautifulSoup, NavigableString, Tag
from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import clean_line


BLUEBOOK_URL = "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"
LIST_SELECTOR = ".scroll-box, .list-box, .left-list"
DETAIL_SELECTOR = ".main-content, .main-content-wrapper, .main-section"
TITLE_RE = re.compile(r"^(国内|全球)(\d{1,2})月(\d{1,2})日(晨会版|午间版|晚间版|全球版)\|\s*(.+)$")
LOGIN_KEYWORDS = ("登录", "验证码", "手机号", "欢迎来到Alpha派", "立即登录")
DISCLAIMER_KEYWORDS = ("免责声明", "免责", "投资建议")
ALPHAPAI_MARKDOWN_DIR = Path(__file__).resolve().parents[2] / "data" / "alphapai_markdown"
DETAIL_SKIP_PREFIXES = ("分享", "播放", "时长")
DETAIL_SKIP_LINES = {
    "根据Alpha派机构投研用户实时研究动态聚合整理生成",
}
SECTION_HEADINGS = {
    "隔夜美股复盘",
    "市场热点",
    "机会前瞻",
}
TIME_LABEL_RE = re.compile(r"^(今天|昨天|\d{2}-\d{2})?\s*\d{2}:\d{2}$")
NUMBERED_HEADING_RE = re.compile(r"^(\d+)[.、]?\s*(.+)$")
HTML_SKIP_TAGS = {"script", "style", "svg", "img", "video", "audio", "iframe"}
HTML_BLOCK_TAGS = {
    "div",
    "p",
    "section",
    "article",
    "header",
    "footer",
    "main",
    "aside",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


def _wait_ready(page, timeout_ms: int = 20000) -> bool:
    deadline = _time.time() + timeout_ms / 1000
    while _time.time() < deadline:
        try:
            body_text = page.locator("body").inner_text(timeout=2000)
            if "Alpha派" not in body_text and "蓝宝书" not in body_text:
                _time.sleep(0.5)
                continue
            list_text = _pick_list_text(page)
            if len(body_text) > 200 and len(list_text) > 50:
                return True
        except Exception:
            pass
        _time.sleep(0.5)
    return False


def _pick_list_text(page) -> str:
    best_text = ""
    try:
        count = page.locator(LIST_SELECTOR).count()
    except Exception:
        return ""
    for idx in range(count):
        try:
            text = page.locator(LIST_SELECTOR).nth(idx).inner_text(timeout=2000)
        except Exception:
            continue
        if "午间版" not in text and "晨会版" not in text and "晚间版" not in text and "全球版" not in text:
            continue
        if len(text) > len(best_text):
            best_text = text
    return best_text


def _fetch_last_published(source_id: str) -> str:
    db_path = Path(__file__).resolve().parents[2] / "data" / "subscriptions.sqlite3"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT published_at FROM rss_entries WHERE source_id = ? ORDER BY published_at DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        conn.close()
        return str(row[0]).strip() if row and row[0] else ""
    except Exception:
        return ""


def looks_like_login_page(page) -> bool:
    try:
        current_url = str(page.url or "").strip().lower()
    except Exception:
        current_url = ""
    try:
        body_text = clean_line(page.locator("body").inner_text(timeout=2000))
    except Exception:
        body_text = ""
    if "/login" in current_url:
        return True
    return any(keyword in body_text for keyword in LOGIN_KEYWORDS)


def _get_existing_keys(source_id: str) -> set[str]:
    db_path = Path(__file__).resolve().parents[2] / "data" / "subscriptions.sqlite3"
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT title FROM rss_entries WHERE source_id = ?", (source_id,)).fetchall()
        conn.close()
        keys = set()
        for (title,) in rows:
            parts = clean_line(str(title or "")).split()
            if len(parts) >= 3:
                keys.add(f"{parts[0]}_{parts[1]}_{parts[2]}")
        return keys
    except Exception:
        return set()


def _parse_time_label(label: str) -> str:
    now = datetime.now()
    label = clean_line(label)
    if re.fullmatch(r"\d{2}-\d{2}\s\d{2}:\d{2}", label):
        month, day = label.split()[0].split("-")
        hhmm = label.split()[1]
        return f"{now.year:04d}/{int(month):02d}/{int(day):02d} {hhmm}"
    if re.fullmatch(r"今天\s+\d{2}:\d{2}", label):
        return now.strftime("%Y/%m/%d ") + label.split()[-1]
    if re.fullmatch(r"昨天\s+\d{2}:\d{2}", label):
        return (now - timedelta(days=1)).strftime("%Y/%m/%d ") + label.split()[-1]
    if re.fullmatch(r"\d{2}:\d{2}", label):
        return now.strftime("%Y/%m/%d ") + label
    return now.strftime("%Y/%m/%d %H:%M")


def _parse_list_entries(text: str) -> List[dict]:
    lines = [clean_line(line) for line in text.splitlines() if clean_line(line)]
    entries: List[dict] = []
    index = 0
    while index < len(lines):
        match = TITLE_RE.match(lines[index])
        if not match:
            index += 1
            continue
        time_label = lines[index + 1] if index + 1 < len(lines) else ""
        region, month, day, edition, summary = match.groups()
        entries.append(
            {
                "region": region,
                "month": month,
                "day": day,
                "edition": edition,
                "summary": summary.strip(),
                "time_label": time_label,
                "published": _parse_time_label(time_label),
                "date": f"{int(month)}月{int(day)}日",
                "raw_title": lines[index],
            }
        )
        index += 2
    return entries


def _extract_list_entries(page) -> List[dict]:
    text = _pick_list_text(page)
    entries = _parse_list_entries(text)
    deduped: List[dict] = []
    seen = set()
    for entry in entries:
        key = f"{entry['region']}_{entry['date']}_{entry['edition']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _click_entry(page, raw_title: str) -> bool:
    candidates = [
        page.locator(".scroll-box").get_by_text(raw_title, exact=True).first,
        page.locator(".list-box").get_by_text(raw_title, exact=True).first,
        page.get_by_text(raw_title, exact=True).first,
    ]
    for locator in candidates:
        try:
            if locator.count() > 0:
                locator.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def _wait_detail_loaded(page, entry: dict, timeout_ms: int = 15000) -> dict[str, str]:
    deadline = _time.time() + timeout_ms / 1000
    title_hint = f"{entry['region']}蓝宝书 {entry['date']} {entry['edition']}"
    summary_hint = entry["summary"][:12]
    while _time.time() < deadline:
        try:
            locator = page.locator(DETAIL_SELECTOR).first
            text = locator.inner_text(timeout=2000)
            if title_hint in text or summary_hint in text:
                return {
                    "text": text,
                    "html": locator.inner_html(timeout=2000),
                }
        except Exception:
            pass
        _time.sleep(0.5)
    return {"text": "", "html": ""}


def _extract_content_title(detail_text: str, fallback_title: str) -> str:
    lines = [clean_line(line) for line in detail_text.splitlines() if clean_line(line)]
    for line in lines[:8]:
        if any(keyword in line for keyword in DISCLAIMER_KEYWORDS):
            continue
        if line.startswith("分享") or line.startswith("播放") or line.startswith("时长"):
            continue
        if len(line) < 6:
            continue
        return line[:180]
    return fallback_title


def _build_entry_title(detail_text: str, fallback_title: str) -> str:
    content_title = _extract_content_title(detail_text, fallback_title)
    lines = [clean_line(line) for line in detail_text.splitlines() if clean_line(line)]
    content_lines: list[str] = []
    for line in lines[1:]:
        if line.startswith("分享") or line.startswith("播放") or line.startswith("时长"):
            continue
        if "根据Alpha派机构投研用户实时研究动态聚合整理生成" in line:
            continue
        if any(keyword in line for keyword in DISCLAIMER_KEYWORDS):
            continue
        content_lines.append(line)
        if len(" ".join(content_lines)) >= 120:
            break
    if not content_lines:
        return content_title[:220]
    summary_excerpt = " ".join(content_lines)
    summary_excerpt = re.sub(r"\s+", " ", summary_excerpt).strip()
    return f"{content_title} | {summary_excerpt[:180]}".strip()[:260]


def _sanitize_filename(value: str) -> str:
    text = clean_line(value).replace("/", "-").replace("\\", "-").replace(":", " -")
    text = re.sub(r'[<>:"/\\|?*]+', "-", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    return text[:120] or "alphapai"


def _extract_detail_lines(detail_text: str, detail_html: str) -> list[str]:
    raw_text = detail_text
    if detail_html.strip():
        soup = BeautifulSoup(detail_html, "html.parser")
        for tag in soup.select(", ".join(HTML_SKIP_TAGS)):
            tag.decompose()

        blocks: list[str] = []

        def render_inline(node: Tag) -> str:
            parts: list[str] = []

            def visit(current) -> None:
                if isinstance(current, NavigableString):
                    text = clean_line(str(current))
                    if text:
                        parts.append(text)
                    return
                if not isinstance(current, Tag):
                    return
                name = current.name.lower()
                if name in HTML_SKIP_TAGS:
                    return
                if name == "br":
                    return
                if name in {"strong", "b"}:
                    text = render_inline(current)
                    if text:
                        parts.append(f"**{text}**")
                    return
                if name in {"em", "i"}:
                    text = render_inline(current)
                    if text:
                        parts.append(f"*{text}*")
                    return
                if name == "a":
                    text = render_inline(current)
                    if text:
                        parts.append(text)
                    return
                for child in current.children:
                    visit(child)

            visit(node)
            return clean_line(" ".join(parts))

        def collect(node: Tag) -> None:
            for child in node.children:
                if isinstance(child, NavigableString):
                    text = clean_line(str(child))
                    if text:
                        blocks.append(text)
                    continue
                if not isinstance(child, Tag):
                    continue
                name = child.name.lower()
                if name in HTML_SKIP_TAGS:
                    continue
                if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    text = render_inline(child)
                    if text:
                        blocks.append(text)
                    continue
                if name == "li":
                    text = render_inline(child)
                    if text:
                        blocks.append(f"- {text}")
                    continue
                if name in {"p", "blockquote"}:
                    text = render_inline(child)
                    if text:
                        blocks.append(text)
                    continue
                if name in {"ul", "ol"}:
                    collect(child)
                    continue
                if name in HTML_BLOCK_TAGS:
                    has_block_children = any(
                        isinstance(grandchild, Tag) and grandchild.name.lower() in HTML_BLOCK_TAGS
                        for grandchild in child.children
                    )
                    if has_block_children:
                        collect(child)
                    else:
                        text = render_inline(child)
                        if text:
                            blocks.append(text)
                    continue
                text = render_inline(child)
                if text:
                    blocks.append(text)

        collect(soup)
        raw_text = "\n".join(blocks) or detail_text

    lines: list[str] = []
    previous = ""
    for raw_line in raw_text.splitlines():
        line = clean_line(raw_line)
        if not line:
            continue
        if line == previous and len(line) <= 32:
            continue
        lines.append(line)
        previous = line
    return lines


def _is_disclaimer_line(line: str) -> bool:
    return any(keyword in line for keyword in DISCLAIMER_KEYWORDS)


def _should_skip_detail_line(line: str) -> bool:
    if line in DETAIL_SKIP_LINES:
        return True
    return any(line.startswith(prefix) for prefix in DETAIL_SKIP_PREFIXES)


def _format_detail_markdown(content_title: str, detail_text: str, detail_html: str, time_label: str) -> str:
    lines = _extract_detail_lines(detail_text, detail_html)
    if not lines:
        return detail_text.strip()

    blocks: list[str] = []
    index = 0
    normalized_time_label = clean_line(time_label)
    seen_title = False

    while index < len(lines):
        line = lines[index]

        if not seen_title and clean_line(content_title) and line == clean_line(content_title):
            seen_title = True
            index += 1
            continue
        if normalized_time_label and line == normalized_time_label:
            index += 1
            continue
        if TIME_LABEL_RE.fullmatch(line):
            index += 1
            continue
        if _should_skip_detail_line(line):
            index += 1
            continue
        if line in SECTION_HEADINGS:
            blocks.append(f"## {line}")
            index += 1
            continue
        if _is_disclaimer_line(line):
            blocks.append(f"> {line}")
            index += 1
            continue
        if re.fullmatch(r"\d{1,2}", line) and index + 1 < len(lines):
            heading_title = lines[index + 1]
            if not _should_skip_detail_line(heading_title):
                blocks.append(f"### {line}. {heading_title}")
                index += 2
                continue
        numbered_heading = NUMBERED_HEADING_RE.match(line)
        if numbered_heading and len(numbered_heading.group(2)) <= 40:
            blocks.append(f"### {numbered_heading.group(1)}. {numbered_heading.group(2)}")
            index += 1
            continue
        if line.startswith("关注："):
            blocks.append(f"**关注：** {line.removeprefix('关注：').strip()}")
            index += 1
            continue
        blocks.append(line)
        index += 1

    return "\n\n".join(blocks).strip()


def _build_markdown_body(
    title: str,
    published: str,
    detail_text: str,
    detail_html: str = "",
    *,
    time_label: str = "",
    display_title: str = "",
) -> str:
    heading = clean_line(display_title) or clean_line(title)
    body = _format_detail_markdown(heading, detail_text, detail_html, time_label)
    return (
        f"# {heading}\n\n"
        f"- 发布时间: {published}\n"
        f"- 来源: Alpha派 蓝宝书\n"
        f"- 原始页: {BLUEBOOK_URL}\n\n"
        f"{body.strip()}\n"
    )


def _write_markdown_file(
    title: str,
    published: str,
    detail_text: str,
    detail_html: str = "",
    *,
    time_label: str = "",
    display_title: str = "",
) -> str:
    ALPHAPAI_MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(f"{published}|{title}".encode("utf-8")).hexdigest()[:12]
    filename = f"{published[:10].replace('/', '-')}_{_sanitize_filename(title)}_{digest}.md"
    path = ALPHAPAI_MARKDOWN_DIR / filename
    path.write_text(
        _build_markdown_body(
            title,
            published,
            detail_text,
            detail_html,
            time_label=time_label,
            display_title=display_title,
        ),
        encoding="utf-8",
    )
    return str(path)


def fetch_alphapai_with_page(page, source: dict, timeout_ms: int = 120000, *, limit: int = 12) -> FeedFetchResult:
    sid = source.get("id", "alphapai")
    sname = source.get("name", "Alpha派蓝宝书")
    feed_url = source.get("feed_url", BLUEBOOK_URL)

    current_url = str(page.url or "").strip()
    if "alphapai-web.rabyte.cn/reading/home/market-report/detail" not in current_url:
        try:
            page.goto(BLUEBOOK_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            return FeedFetchResult(sid, sname, feed_url, False, 0, [], f"页面加载失败: {exc}")

    if looks_like_login_page(page):
        return FeedFetchResult(sid, sname, feed_url, False, 401, [], "蓝宝书登录态失效")

    if not _wait_ready(page, timeout_ms=min(timeout_ms, 20000)):
        if looks_like_login_page(page):
            return FeedFetchResult(sid, sname, feed_url, False, 401, [], "蓝宝书登录态失效")
        return FeedFetchResult(sid, sname, feed_url, False, 0, [], "蓝宝书页面未完成加载")

    try:
        raw_entries = _extract_list_entries(page)
    except Exception as exc:
        return FeedFetchResult(sid, sname, feed_url, False, 0, [], f"列表解析失败: {exc}")

    if not raw_entries:
        if looks_like_login_page(page):
            return FeedFetchResult(sid, sname, feed_url, False, 401, [], "蓝宝书登录态失效")
        return FeedFetchResult(sid, sname, feed_url, False, 200, [], "页面可访问，但未解析到报告条目")

    existing_keys = _get_existing_keys(sid)
    candidate_entries = raw_entries[:limit] if limit and limit > 0 else raw_entries
    new_entries = [entry for entry in candidate_entries if f"{entry['region']}_{entry['date']}_{entry['edition']}" not in existing_keys]

    if not new_entries:
        return FeedFetchResult(sid, sname, feed_url, True, 200, [], "")

    results: List[FeedEntry] = []
    for entry in new_entries:
        detail_text = ""
        detail_html = ""
        fallback_title = f"{entry['region']} {entry['date']} {entry['edition']}"
        try:
            if _click_entry(page, entry["raw_title"]):
                detail_payload = _wait_detail_loaded(page, entry, timeout_ms=10000)
                detail_text = detail_payload.get("text", "")
                detail_html = detail_payload.get("html", "")
        except Exception as exc:
            detail_text = f"[详情提取失败: {exc}]"

        if not detail_text:
            detail_text = entry["summary"]

        content_title = _extract_content_title(detail_text, fallback_title)
        title = _build_entry_title(detail_text, fallback_title)
        markdown_path = _write_markdown_file(
            title,
            entry["published"],
            detail_text.strip(),
            detail_html,
            time_label=entry["time_label"],
            display_title=content_title,
        )

        results.append(
            FeedEntry(
                source_id=sid,
                source_name=sname,
                title=title,
                link=BLUEBOOK_URL,
                published=entry["published"],
                summary=detail_text.strip(),
                markdown_path=markdown_path,
            )
        )

    return FeedFetchResult(
        source_id=sid,
        source_name=sname,
        feed_url=feed_url,
        ok=bool(results),
        status=200 if results else 0,
        entries=results,
        error="" if results else "未能提取任何有效内容",
    )

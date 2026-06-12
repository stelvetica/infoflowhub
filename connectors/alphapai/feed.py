from __future__ import annotations

import re
import time as _time
from datetime import datetime
from typing import List

from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared.common import clean_line, normalize_relative_date


BLUEBOOK_URL = "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"
BODY_SELECTOR = "div.app-layout-body"
DISCLAIMER_KEYS = ("免责", "免责申明", "申明", "不构成任何投资建议", "投资建议")

# 列表条目正则： (全球|国内)月日版别| 内容 时间
ENTRY_RE = re.compile(
    r"(全球|国内)(\d+)月(\d+)日((?:全球版|晨会版|晚间版|午间版))\|\s*"
    r"([\s\S]+?)"
    r"(今天|昨天|\d{1,2}:\d{1,2}|\d{1,2}-\d{1,2}\s\d{1,2}:\d{1,2})"
)


def _now_text() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M")


def _entry_date_key(entry: dict) -> str:
    """生成可排序的日期键：MM-DD"""
    return f"{int(entry['month']):02d}-{int(entry['day']):02d}"


def _entry_published(entry: dict) -> str:
    """将「6月12日 07:11」转为规范格式「2026/06/12 07:11」"""
    now = datetime.now()
    month = int(entry["month"])
    day = int(entry["day"])
    time_str = entry.get("time", "00:00")
    # 处理「今天」「昨天」
    if time_str == "今天":
        time_str = "00:00"
    elif time_str == "昨天":
        time_str = "00:00"
        day = day  # keep as-is from regex
    # 处理 "06-10 20:06" 格式
    m = re.match(r"\d{1,2}-\d{1,2}\s(\d{2}:\d{2})", time_str)
    if m:
        time_str = m.group(1)
    if not re.match(r"\d{2}:\d{2}", time_str):
        time_str = "00:00"
    return f"{now.year:04d}/{month:02d}/{day:02d} {time_str}"


def _fetch_last_published(source_id: str) -> str:
    """从 SQLite 查该源最近一条入库时间"""
    import sqlite3
    from pathlib import Path

    db_path = Path(__file__).resolve().parents[2] / "data" / "subscriptions.sqlite3"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            "SELECT published_at FROM rss_entries WHERE source_id = ? ORDER BY published_at DESC LIMIT 1",
            (source_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return str(row[0]).strip()
        return ""
    except Exception:
        return ""


def _last_date_stop(last_published: str) -> str:
    """将「2026/06/11 00:00」转为「06-11」格式用于停止判定"""
    if not last_published:
        return ""
    m = re.match(r"\d{4}/(\d{2})/(\d{2})", last_published)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


# ====================================================================
# SPA 稳定
# ====================================================================

def _wait_spa(page, timeout: int = 15000) -> bool:
    """等待 SPA 渲染：body 容器出现 + DOM 长度稳定"""
    start = _time.time()
    try:
        page.wait_for_selector(BODY_SELECTOR, state="attached", timeout=timeout)
    except Exception:
        pass

    last_len = 0
    stable = 0
    deadline = start + timeout / 1000
    while _time.time() < deadline:
        try:
            cur_len = len(page.locator(BODY_SELECTOR).inner_text())
        except Exception:
            cur_len = 0
        if cur_len > 100 and cur_len == last_len:
            stable += 1
        else:
            stable = 0
            last_len = cur_len
        if stable >= 3:
            return True
        _time.sleep(0.5)
    return last_len > 100


# ====================================================================
# 列表提取
# ====================================================================

def _scroll_collect(page, stop_date: str) -> List[dict]:
    """滚动加载条目直到连续 2 条日期 ≤ stop_date"""
    entries: List[dict] = []
    seen = set()
    stop_count = 0

    for _ in range(30):  # 最多滚 30 次
        try:
            raw = page.locator(BODY_SELECTOR).inner_text()
        except Exception:
            raw = ""
        raw = raw.replace("\n", " ").replace("\r", " ")
        new_entries = _parse_entries(raw)
        for e in new_entries:
            key = f"{e['region']}|{e['date']}|{e['edition']}|{e['summary'][:30]}"
            if key not in seen:
                seen.add(key)
                entries.append(e)

        # 停止检查
        if stop_date:
            for e in entries[-4:]:  # 检查最后 4 条
                if _entry_date_key(e) <= stop_date:
                    stop_count += 1
                else:
                    stop_count = 0
                if stop_count >= 2:
                    return entries

        # 滚到底
        try:
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        except Exception:
            pass
        _time.sleep(0.8)

        # 检测是否见底
        try:
            body_text = page.locator(BODY_SELECTOR).inner_text()
            if "蓝宝书介绍" in body_text:
                _time.sleep(0.5)
                # 再滚一次看有无新内容
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                _time.sleep(0.8)
                new_text = page.locator(BODY_SELECTOR).inner_text()
                if new_text == body_text:
                    break  # 真的到底了
        except Exception:
            pass

    return entries


def _parse_entries(text: str) -> List[dict]:
    """正则提取条目"""
    entries = []
    for m in ENTRY_RE.finditer(text):
        entries.append({
            "region": m.group(1),
            "month": m.group(2),
            "day": m.group(3),
            "edition": m.group(4),
            "summary": m.group(5).strip(),
            "time": m.group(6),
            "date": f"{m.group(2)}月{m.group(3)}日",
        })
    return entries


# ====================================================================
# 详情提取
# ====================================================================

def _find_clickable(page, summary: str):
    """通过文本片段定位可点击元素"""
    keyword = summary[:15].replace('"', '\\"').replace("'", "\\'")
    try:
        el = page.locator(f"xpath=//*[contains(text(),'{keyword}')]").first
        if not el:
            return None
        tag = el.evaluate("el => el.tagName.toLowerCase()")
        cls = el.evaluate("el => el.className || ''")
        cls_str = str(cls).strip() if cls else ""
        if cls_str:
            sel = tag + "." + cls_str.split()[0]
            parent = el.locator("..")
            try:
                if parent.locator(f":scope > {sel}").count() > 1:
                    return parent
            except Exception:
                pass
        return el
    except Exception:
        return None


def _wait_disclaimer(page, timeout: int = 15000) -> str:
    """等待免责声明出现，返回 body 文本"""
    start = _time.time()
    while _time.time() - start < timeout / 1000:
        try:
            text = page.locator(BODY_SELECTOR).inner_text()
            if any(k in text for k in DISCLAIMER_KEYS):
                return text
        except Exception:
            pass
        _time.sleep(0.5)
    return ""


def _find_detail_container(page):
    """精确定位详情容器"""
    markers = ("分享播放时长", "播放时长", "聚合并生成", "市场热点", "机会前瞻")
    try:
        all_els = page.locator("div, section, article").all()
        best = None
        best_score = 0
        for el in all_els:
            try:
                txt = el.inner_text()
            except Exception:
                continue
            if len(txt) < 500 or len(txt) > 60000:
                continue
            if txt.startswith("全部国内全球"):
                continue
            score = sum(1 for m in markers if m in txt)
            if score > best_score:
                best_score = score
                best = el
        return best
    except Exception:
        return None


def _html_to_markdown(html: str) -> str:
    """HTML → Markdown 保留格式"""
    import html as _html_module

    text = html

    # 处理常见的块级结构
    # h1-h6 → ##
    text = re.sub(r"<h1[^>]*>(.*?)</h1>", r"\n## \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n### \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h[4-6][^>]*>(.*?)</h[4-6]>", r"\n#### \1\n", text, flags=re.DOTALL)
    # <p> → 双换行
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\n\1\n", text, flags=re.DOTALL)
    # <br> → 换行
    text = re.sub(r"<br\s*/?>", "\n", text)
    # <li> → -
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL)
    # 加粗
    text = re.sub(r"<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>", r"**\1**", text, flags=re.DOTALL)
    # 斜体
    text = re.sub(r"<(?:em|i)[^>]*>(.*?)</(?:em|i)>", r"*\1*", text, flags=re.DOTALL)
    # <hr>
    text = re.sub(r"<hr\s*/?>", "\n---\n", text)
    # 移除其余 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # HTML 实体
    text = _html_module.unescape(text)
    # 清理多余空白
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"^[ \t]+", "", text, flags=re.MULTILINE)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _go_back(page):
    """点击返回/关闭按钮"""
    selectors = (
        '[class*="back"]', '[class*="close"]', ".el-icon-close",
        ".el-drawer__close-btn", ".el-icon-arrow-left",
    )
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn and btn.is_visible():
                btn.click()
                _time.sleep(2)
                return
        except Exception:
            continue
    # 兜底：ESC
    page.keyboard.press("Escape")
    _time.sleep(2)


# ====================================================================
# 主入口
# ====================================================================

def fetch_alphapai_with_page(page, source: dict, timeout_ms: int = 120000, *, limit: int = 12) -> FeedFetchResult:
    """
    使用已有 page 获取蓝宝书内容。

    参数:
        page: Playwright Page（调用方负责导航到目标URL）
        source: 订阅源字典
        timeout_ms: 超时毫秒（默认 2 分钟）
        limit: 最大抓取条数（0 = 不限制）

    返回:
        FeedFetchResult
    """
    sid = source.get("id", "alphapai")
    sname = source.get("name", "Alpha派蓝宝书")
    feed_url = source.get("feed_url", BLUEBOOK_URL)

    # 1. 导航 + 等 SPA
    try:
        page.goto(BLUEBOOK_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        return FeedFetchResult(
            source_id=sid, source_name=sname, feed_url=feed_url,
            ok=False, status=0, entries=[], error=f"页面加载失败: {exc}",
        )

    if not _wait_spa(page):
        return FeedFetchResult(
            source_id=sid, source_name=sname, feed_url=feed_url,
            ok=False, status=0, entries=[],
            error="SPA 渲染超时，未找到内容容器",
        )

    # 2. 查上次最晚日期
    last_pub = _fetch_last_published(sid)
    stop_date = _last_date_stop(last_pub)

    # 3. 滚动收集列表
    raw_entries = _scroll_collect(page, stop_date)

    if not raw_entries:
        return FeedFetchResult(
            source_id=sid, source_name=sname, feed_url=feed_url,
            ok=False, status=200, entries=[],
            error="页面可访问，但未解析到报告条目",
        )

    # 4. 过滤已存在条目
    existing_keys = _get_existing_keys(sid)
    new_entries = [
        e for e in raw_entries
        if f"{e['region']}_{e['date']}_{e['edition']}" not in existing_keys
    ]

    # limit 限制
    if limit and limit > 0:
        new_entries = new_entries[:limit]

    if not new_entries:
        return FeedFetchResult(
            source_id=sid, source_name=sname, feed_url=feed_url,
            ok=True, status=200, entries=[],
            error="",
        )

    # 5. 逐条点入详情（最多 limit 条）
    results: List[FeedEntry] = []
    for e in new_entries:
        full_content = ""
        try:
            clickable = _find_clickable(page, e["summary"])
            if clickable:
                clickable.click()
                detail_html = _wait_disclaimer(page)
                if detail_html:
                    container = _find_detail_container(page)
                    source_html = container.inner_html() if container else detail_html
                    full_content = _html_to_markdown(source_html)
                _go_back(page)
        except Exception as exc:
            full_content = f"[提取失败: {exc}]"
            try:
                _go_back(page)
            except Exception:
                page.goto(BLUEBOOK_URL, wait_until="domcontentloaded", timeout=15000)
                _wait_spa(page)

        if not full_content:
            full_content = e["summary"]

        results.append(FeedEntry(
            source_id=sid,
            source_name=sname,
            title=f"{e['region']} {e['date']} {e['edition']}",
            link=BLUEBOOK_URL,
            published=_entry_published(e),
            summary=full_content,
        ))

    return FeedFetchResult(
        source_id=sid,
        source_name=sname,
        feed_url=feed_url,
        ok=bool(results),
        status=200 if results else 0,
        entries=results,
        error="" if results else "未能提取任何有效内容",
    )


def _get_existing_keys(source_id: str) -> set:
    """获取已入库的 (region, date, edition) 集合"""
    import sqlite3
    from pathlib import Path

    db_path = Path(__file__).resolve().parents[2] / "data" / "subscriptions.sqlite3"
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            "SELECT title FROM rss_entries WHERE source_id = ?",
            (source_id,),
        )
        keys = set()
        for row in cur.fetchall():
            title = str(row[0] or "")
            m = re.match(r"(\S+)\s+\S+\s+\S+", title)
            if m:
                # "全球 6月12日 全球版" → "全球_6月12日_全球版"
                parts = title.split()
                if len(parts) >= 3:
                    keys.add(f"{parts[0]}_{parts[1]}_{parts[2]}")
            else:
                keys.add(clean_line(title))
        conn.close()
        return keys
    except Exception:
        return set()

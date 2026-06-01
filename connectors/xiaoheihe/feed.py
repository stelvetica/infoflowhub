from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from apps.laterhub.config import DEBUG_DIR
from connectors._shared.common import USER_AGENT
from connectors.auth import get_auth_context_path
from connectors.douyin.favorites import _resolve_default_browser_executable


XIAOHEIHE_FAVOR_URL = "https://www.xiaoheihe.cn/app/user/favour/content"
XIAOHEIHE_POST_PATTERN = re.compile(r"/app/bbs/(link|detail)/(\w+)")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_JSON_PATH = DEBUG_DIR / "tmp_pw_xiaoheihe_favorites.json"


def normalize_xiaoheihe_item(item: dict[str, Any]) -> dict[str, Any] | None:
    url = (item.get("url") or "").strip().split("?")[0]
    raw_title = (item.get("title") or "").strip()
    link_id = (item.get("link_id") or "").strip()

    if not url or not link_id:
        return None
    if not url.startswith("https://www.xiaoheihe.cn/app/bbs/"):
        return None

    title = re.sub(r"\s+", " ", raw_title).strip()
    if not title:
        title = f"小黑盒帖子_{link_id[:8]}"
    if title.startswith("帖子_"):
        return None

    return {
        "url": url,
        "title": title,
        "source": "xiaoheihe_favor",
        "owner": "",
        "tags": None,
        "raw": item,
    }


def _extract_visible_items(page, *, scroll_times: int = 5, scroll_pause_ms: int = 2000) -> list[dict[str, Any]]:
    for i in range(scroll_times):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(scroll_pause_ms)

    js = r"""
    () => {
      const results = [];
      const seen = new Set();
      const anchors = Array.from(document.querySelectorAll('a[href*="/app/bbs/"]'));
      for (const a of anchors) {
        const href = (a.href || '').split('?')[0];
        const match = href.match(/\/app\/bbs\/(link|detail)\/(\w+)/);
        if (!match) continue;
        const linkId = match[2];
        if (seen.has(linkId)) continue;
        seen.add(linkId);

        let title = '';
        const card = a.closest('[class*="card"]')
                  || a.closest('[class*="item"]')
                  || a.closest('[class*="post"]')
                  || a.closest('[class*="feed"]')
                  || a.closest('li')
                  || a.closest('article')
                  || a.parentElement;
        if (card) {
          const titleEl = card.querySelector(
            '[class*="title"], h1, h2, h3, h4, h5, h6, .content-title, .feed-title, .post-title, [class*="subject"]'
          );
          if (titleEl) {
            title = (titleEl.textContent || '').trim();
          }
        }
        if (!title) {
          const text = (a.textContent || '').trim();
          if (text.length < 200) {
            title = text;
          }
        }
        if (!title) title = '';

        results.push({
          link_id: linkId,
          url: 'https://www.xiaoheihe.cn/app/bbs/link/' + linkId,
          title: title.slice(0, 200),
          raw_text: card ? (card.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 1000) : '',
        });
      }
      return results;
    }
    """
    return page.evaluate(js)


def _extract_items_via_api(page, *, timeout_ms: int = 10000) -> list[dict[str, Any]] | None:
    """Try to intercept the favorites API response."""
    api_responses: list[dict[str, Any]] = []

    def capture(response):
        url = response.url
        if "api.xiaoheihe.cn" in url and "favour" in url.lower():
            try:
                body = response.json()
                api_responses.append({"url": url, "body": body})
            except Exception:
                pass

    page.on("response", capture)
    page.goto(XIAOHEIHE_FAVOR_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3000)

    if not api_responses:
        return None

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resp in api_responses:
        body = resp["body"]
        result = body.get("result") or body.get("data") or body
        if isinstance(result, dict):
            candidates = result.get("links") or result.get("list") or result.get("items") or [result]
        elif isinstance(result, list):
            candidates = result
        else:
            continue
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            link_id = (
                entry.get("link_id")
                or entry.get("linkid")
                or entry.get("id")
                or entry.get("object_id")
                or ""
            )
            link_id = str(link_id).strip()
            if not link_id or link_id in seen:
                continue
            seen.add(link_id)
            title = str(entry.get("title") or entry.get("content_title") or entry.get("name") or "").strip()
            url = f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}"
            items.append({
                "link_id": link_id,
                "url": url,
                "title": title[:200] if title else f"小黑盒帖子_{link_id[:8]}",
                "raw_text": "",
            })
    return items if items else None


def fetch_xiaoheihe_favorites(env_path: str | Path | None = None) -> list[dict[str, Any]]:
    profile_dir = get_auth_context_path("xiaoheihe_shared")
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        _, browser_executable = _resolve_default_browser_executable()
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=browser_executable,
            headless=False,
            args=["--new-window", "--window-size=1440,960"],
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        # Try API interception first, fall back to DOM extraction
        items = _extract_items_via_api(page)
        if not items:
            page.goto(XIAOHEIHE_FAVOR_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            items = _extract_visible_items(page, scroll_times=5, scroll_pause_ms=2000)

        DEBUG_JSON_PATH.write_text(
            json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        context.close()

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        normalized = normalize_xiaoheihe_item(item)
        if not normalized:
            continue
        url = normalized["url"]
        if url in seen:
            continue
        seen.add(url)
        result.append(normalized)
    return result

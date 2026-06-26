from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from apps.laterhub.config import DEBUG_DIR
from connectors._shared.chrome_runner import SharedRunnerSession
from connectors._shared.common import CHROME_USER_DATA, USER_AGENT
from connectors.auth import get_auth_context_path


DOUYIN_WEAK_TITLE_PATTERNS = [
    re.compile(r"^.{0,40}?\d{8}发布的作品$"),
    re.compile(r"^.{0,40}?发布的作品$"),
]
DOUYIN_FAVORITE_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_JSON_PATH = DEBUG_DIR / "tmp_pw_douyin_favorites.json"


def normalize_douyin_item(item: dict[str, Any]) -> dict[str, Any] | None:
    url = (item.get("url") or "").strip().split("?")[0]
    raw_title = (item.get("title") or item.get("raw_text") or "").strip()
    raw_text = (item.get("raw_text") or "").strip()

    if not url.startswith("https://www.douyin.com/video/"):
        return None
    if raw_title.startswith("热门") or raw_text.startswith("热门"):
        return None

    title = re.sub(r"\s+", " ", raw_title).strip() or "未命名内容"
    if re.fullmatch(r"视频_\d+", title):
        return None

    return {
        "url": url,
        "title": title,
        "source": "douyin_favorite",
        "owner": "",
        "tags": None,
        "raw": item,
    }


def _extract_visible_items(page) -> list[dict[str, Any]]:
    js = r"""
    () => {
      const results = [];
      const seen = new Set();
      const anchors = Array.from(document.querySelectorAll('a[href*="/video/"]'));
      for (const a of anchors) {
        const href = (a.href || '').split('?')[0];
        const match = href.match(/\/video\/(\d+)/);
        if (!match) continue;
        const videoId = match[1];
        if (seen.has(videoId)) continue;
        seen.add(videoId);
        let title = '';
        const card = a.closest('li') || a.closest('[class*="item"]') || a.parentElement;
        if (card) {
          const img = card.querySelector('img');
          if (img && img.alt && !/^[\d.万亿]+$/.test(img.alt)) {
            title = img.alt.trim();
          }
        }
        if (!title) title = `视频_${videoId}`;
        results.push({
          video_id: videoId,
          url: href,
          title,
          raw_text: card ? (card.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 1000) : '',
        });
      }
      return results;
    }
    """
    return page.evaluate(js)


def _resolve_douyin_source_profile_dir() -> Path:
    shared_profile_dir = get_auth_context_path("douyin_shared") / "Default"
    default_profile_dir = CHROME_USER_DATA / "Default"
    if (shared_profile_dir / "Network" / "Cookies").exists() and (shared_profile_dir / "Preferences").exists():
        return shared_profile_dir
    return default_profile_dir


def fetch_douyin_favorites(*args, session: SharedRunnerSession | None = None, **kwargs) -> list[dict[str, Any]]:
    own_session = session is None
    if own_session:
        from connectors.auth.providers.browser_profiles import AUTH_PROFILE_DIR
        session = SharedRunnerSession(
            source_profile_dir=AUTH_PROFILE_DIR,
            extra_args=[f"--user-agent={USER_AGENT}", "--lang=zh-CN,zh;q=0.9,en;q=0.8"],
        )
        session.start()

    items: list[dict[str, Any]] = []
    try:
        page = session.acquire_page()
        page.goto(DOUYIN_FAVORITE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        items = _extract_visible_items(page)
        try:
            page.close()
        except Exception:
            pass
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_JSON_PATH.write_text(
            json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        if own_session:
            session.shutdown()

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        normalized = normalize_douyin_item(item)
        if not normalized:
            continue
        url = normalized["url"]
        if url in seen:
            continue
        seen.add(url)
        result.append(normalized)
    return result




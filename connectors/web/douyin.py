from __future__ import annotations

import json
import re
import winreg
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from apps.laterhub.config import DEBUG_DIR, PW_DOUYIN_PROFILE


DOUYIN_WEAK_TITLE_PATTERNS = [
    re.compile(r"^.{0,40}?\d{8}发布的作品$"),
    re.compile(r"^.{0,40}?发布的作品$"),
]
DOUYIN_FAVORITE_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_JSON_PATH = DEBUG_DIR / "tmp_pw_douyin_favorites.json"
WINDOWS_BROWSER_PATHS = {
    "ChromeHTML": [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ],
    "MSEdgeHTM": [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ],
    "BraveHTML": [
        Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ],
    "VivaldiHTM": [
        Path(r"C:\Program Files\Vivaldi\Application\vivaldi.exe"),
        Path(r"C:\Program Files (x86)\Vivaldi\Application\vivaldi.exe"),
    ],
}


def _resolve_default_browser_executable() -> tuple[str, str]:
    prog_id = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        prog_id = ""

    for candidate in WINDOWS_BROWSER_PATHS.get(prog_id, []):
        if candidate.exists():
            return prog_id or "unknown", str(candidate)

    for fallback_prog_id in ("ChromeHTML", "MSEdgeHTM", "BraveHTML", "VivaldiHTM"):
        for candidate in WINDOWS_BROWSER_PATHS[fallback_prog_id]:
            if candidate.exists():
                return fallback_prog_id, str(candidate)

    raise RuntimeError("未找到可供 Playwright 启动的 Chromium 浏览器，请先安装 Chrome、Edge、Brave 或 Vivaldi")


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


def fetch_douyin_favorites(*args, **kwargs) -> list[dict[str, Any]]:
    PW_DOUYIN_PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        _, browser_executable = _resolve_default_browser_executable()
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PW_DOUYIN_PROFILE),
            executable_path=browser_executable,
            headless=False,
            args=["--new-window"],
        )
        page = context.new_page()
        page.goto(DOUYIN_FAVORITE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        items = _extract_visible_items(page)
        DEBUG_JSON_PATH.write_text(
            json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        context.close()

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

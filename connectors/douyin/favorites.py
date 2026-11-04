from __future__ import annotations

import json
import re
from typing import Any

from apps.laterhub.config import DEBUG_DIR
from connectors._shared.bsk_session import BskRunnerSession
from connectors.douyin.feed import DOUYIN_LOGIN_WALL_MARKERS


DOUYIN_WEAK_TITLE_PATTERNS = [
    re.compile(r"^.{0,40}?\d{8}发布的作品$"),
    re.compile(r"^.{0,40}?发布的作品$"),
]
DOUYIN_FAVORITE_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"
# 收藏列表懒加载：首屏只渲染一部分，要靠**真实滚轮事件**往下带（收藏页是内层滚动容器，
# window.scrollTo 几乎无效——实测锚点 scrollTo 后 36 个，wheel×3 到 134 个）。轮数上限有意
# 留着：不做全量，滚越多越慢、也越容易触发风控；滚到「本轮条数不再增长」即提前停。
DOUYIN_SCROLL_ROUNDS = 3
DOUYIN_SCROLL_STEP_PX = 3000
DOUYIN_SCROLL_WAIT_MS = 2500
# 收藏页卡片会在滚轮悬停时自动播放预览视频。bsk 的 wheel/evaluate 属无目标动作，strict 策略
# 对它们保持整页基线校验：一旦有视频在播（媒体源持续变化），后续动作一律
# `permission_denied: The media source changed` / `observed content changed`（实测裸滚 8/8 被拒，
# 2026-11-04 07:14 抖音频道的整源失败即此）。滚动前先装一个停播器，把页面压成静态后再滚。
DOUYIN_STABILIZE_MEDIA_JS = r"""
() => {
  if (window.__bskPauseVideos) return "already";
  window.__bskPauseVideos = true;
  const pause = () => document.querySelectorAll("video").forEach((v) => {
    try { if (!v.paused) v.pause(); v.autoplay = false; } catch (e) {}
  });
  pause();
  setInterval(pause, 250);
  return "installed";
}
"""
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
    value = page.evaluate(js)
    if not isinstance(value, list):
        # 信任边界校验：页内函数没被真正执行时 bsk 会回 {}（实测，函数按值序列化），
        # 静默当 0 条会把「提取失败」伪装成「没有收藏」。
        raise RuntimeError(f"抖音收藏页内提取返回异常类型: {type(value).__name__}")
    return value


def _body_text(page) -> str:
    # 用一次 evaluate 取 body 文本（BskPage 无 locator.inner_text 之外更轻的取法）。
    value = page.evaluate("document.body ? document.body.innerText : ''")
    return str(value or "")


def fetch_douyin_favorites(
    env_path: str | None = None,
    *,
    session: BskRunnerSession | None = None,
) -> list[dict[str, Any]]:
    # ponytail: env_path 仅为与 pipeline 的 fetcher(ENV_PATH, session=...) 调用姿势同形，
    # bsk 走用户已登录的浏览器，不需要 env 配置；若将来 bsk 需要 env 参数再接线。
    own_session = session is None
    if own_session:
        session = BskRunnerSession(start_url="about:blank")
        session.start()

    items: list[dict[str, Any]] = []
    body_text = ""
    try:
        page = session.acquire_page()
        page.goto(DOUYIN_FAVORITE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        # 先停掉预览视频再滚：滚动会触发卡片自动播放，媒体源一变后续 wheel/evaluate 全被拒。
        page.evaluate(DOUYIN_STABILIZE_MEDIA_JS)
        items = _extract_visible_items(page)
        # 逐轮下滚让懒加载条目进入 DOM；某轮条数不再增长就停。
        for _ in range(DOUYIN_SCROLL_ROUNDS):
            page.mouse.wheel(0, DOUYIN_SCROLL_STEP_PX)
            page.wait_for_timeout(DOUYIN_SCROLL_WAIT_MS)
            grown = _extract_visible_items(page)
            if len(grown) <= len(items):
                break
            items = grown
        body_text = _body_text(page)
        try:
            page.close()
        except Exception:
            pass
    finally:
        if own_session:
            session.shutdown()

    login_wall = not items and any(marker in body_text for marker in DOUYIN_LOGIN_WALL_MARKERS)
    # 调试产物的 ok 必须反映真实结论：登录墙时写 ok=false 并带失败原因，不能先写成功产物再抛错。
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_JSON_PATH.write_text(
        json.dumps(
            {
                "ok": not login_wall,
                "count": len(items),
                "items": items,
                **({"error": "命中登录墙标记，未能加载收藏列表"} if login_wall else {}),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if login_wall:
        # 失败必须可见：0 条 + 登录墙标记时不静默返回空列表，抛错让 pipeline 记成可见失败。
        raise RuntimeError("抖音收藏页面疑似未登录/登录态失效：命中登录墙标记，未能加载收藏列表")

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

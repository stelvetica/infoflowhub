from __future__ import annotations

import json
import random
import re
from typing import Any

from apps.laterhub.config import DEBUG_DIR
from connectors._shared.bsk_browser import BskCommandError
from connectors._shared.bsk_session import BskRunnerSession
from connectors._shared.common import FetchBatch
from connectors.douyin.feed import DOUYIN_LOGIN_WALL_MARKERS


DOUYIN_WEAK_TITLE_PATTERNS = [
    re.compile(r"^.{0,40}?\d{8}发布的作品$"),
    re.compile(r"^.{0,40}?发布的作品$"),
]
DOUYIN_FAVORITE_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"
# 收藏列表懒加载：首屏只渲染一部分，要靠**真实滚轮事件**往下带（收藏页是内层滚动容器，
# window.scrollTo 几乎无效——实测锚点 scrollTo 后 36 个，wheel×3 到 134 个）。
# 两种节奏：日常增量只为抓新，滚大步长、轮数有上限；到期深度全量要拿到完整列表（据此判定
# 「源内已移除」），步长必须小于一屏，否则虚拟列表会跳过未渲染的卡片。
DOUYIN_SCROLL_ROUNDS = 10              # 增量模式轮数上限
DOUYIN_DEEP_SCROLL_MAX_ROUNDS = 150    # 深度全量上限；撞上限视为没拿全（与 30 分钟预算对齐）
DOUYIN_SCROLL_EMPTY_ROUNDS = 2         # 连续零新增轮数（增量模式收工条件/深度模式确认条件）
# 日常短抓取收工条件：连续这么多轮「没有未入库条目」才算追平历史。
DOUYIN_STALE_ROUNDS = 2
# 深度模式的「到底」必须由滚动几何证明：收藏页是内层滚动容器，滚轮没落到容器上时
# 卡片不会新增，旧逻辑（连续两轮无新增即到底）会把「滚动卡住」当成「扫完了」，
# 进而把仍在收藏里的条目整批误标「源内已移除」（2026-11-08 误标 780 条即此）。
DOUYIN_BOTTOM_TOLERANCE_PX = 48
# 深度模式连续零新增且滚动位置不再前进的轮数：说明滚不动了（容器选错/页面卡住），
# 按「未拿全」收工，绝不据此判定到底。
DOUYIN_SCROLL_STALL_ROUNDS = 6
DOUYIN_SCROLL_STATE_JS = r"""
() => {
  // 收藏页是内层滚动容器：取「可滚动距离最大」的元素，别拿整页 document 顶替。
  let best = document.scrollingElement || document.documentElement;
  let bestDelta = best ? best.scrollHeight - best.clientHeight : 0;
  for (const n of document.querySelectorAll("div,main,section,ul")) {
    const delta = n.scrollHeight - n.clientHeight;
    if (delta > bestDelta + 4 && n.clientHeight > 200) {
      best = n;
      bestDelta = delta;
    }
  }
  if (!best) return null;
  return { top: best.scrollTop, height: best.scrollHeight, client: best.clientHeight };
}
"""
DOUYIN_SCROLL_STEP_PX = 3000           # 增量步长：只为抓新的，滚大一点快
DOUYIN_DEEP_SCROLL_STEP_PX = 900       # 深度步长：小于一屏，避免虚拟列表跳过卡片
DOUYIN_SCROLL_WAIT_BASE_MS = 2500
DOUYIN_SCROLL_WAIT_STEP_MS = 300       # 越滚越慢：轮次基数递增
DOUYIN_SCROLL_WAIT_JITTER_MS = 1500    # 在基数上叠加随机抖动
DOUYIN_SCROLL_WAIT_MAX_MS = 10000
# 收藏页卡片会在滚轮悬停时自动播放预览视频。bsk 的 wheel/evaluate 属无目标动作，strict 策略
# 对它们保持整页基线校验：一旦有视频在播（媒体源持续变化），后续动作一律
# `permission_denied: The media source changed` / `observed content changed`（实测裸滚 8/8 被拒，
# 2026-11-04 07:14 与 2026-11-09 07:13 的整源失败即此）。只靠「发现播放再 pause」不够：
# 每轮滚动都会新挂载卡片并起播，250ms 轮询的空窗期足以让 sync→wheel 之间媒体变更被拒。
# 故直接封死 play()，并用 MutationObserver 在卡片挂载瞬间压住，让页面全程保持静态。
DOUYIN_STABILIZE_MEDIA_JS = r"""
() => {
  // 幂等标记挂在被替换的函数上，而不是 window：bsk 复用标签页且抖音是 SPA，
  // 同一 URL 二次进入可能不重建 window，window 上的旧标记会让新逻辑整段跳过（实测导致滚轮持续被拒）。
  if (HTMLMediaElement.prototype.play.__bskBlocked) return "already";
  const noPlay = function () { return Promise.resolve(); };
  noPlay.__bskBlocked = true;
  try { HTMLMediaElement.prototype.play = noPlay; } catch (e) {}
  const pause = () => document.querySelectorAll("video,audio").forEach((v) => {
    try { if (!v.paused) v.pause(); v.autoplay = false; v.muted = true; } catch (e) {}
  });
  pause();
  new MutationObserver(pause).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(pause, 500);
  return "installed";
}
"""
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_JSON_PATH = DEBUG_DIR / "tmp_pw_douyin_favorites.json"


def _install_stabilizer(page, *, attempts: int = 3) -> None:
    """装停播器，把收藏页压成静态再滚。

    页面自播起播后连首个 evaluate 都会被 strict 策略拒（`permission_denied`，
    2026-11-09 07:13 的整源失败即此）：等一拍让页面本身停稳再重试。
    非权限类错误不重试，避免把真故障拖成三次等待。
    """
    for attempt in range(attempts):
        try:
            page.evaluate(DOUYIN_STABILIZE_MEDIA_JS)
            return
        except BskCommandError as exc:
            if attempt + 1 >= attempts or "permission_denied" not in str(exc):
                raise
            page.wait_for_timeout(1000)


def _fallback_title(raw_text: str, kind: str, item_id: str) -> str:
    """卡片标题取不到时，从卡片正文里凑一个可读标题。

    收藏卡片正文以播放量开头（如「1.1万 标题…」），先剥掉计数再取前一段。
    实在没有正文才用占位标题——**不再丢条目**：标题缺失只说明卡片没渲染完，
    不能据此判定这条收藏不存在（旧逻辑会把 19% 的条目直接丢掉）。
    """
    text = re.sub(r"^[\d.]+[万亿]?\s*", "", re.sub(r"\s+", " ", raw_text or "")).strip()
    if text:
        return text[:120]
    return f"抖音{'图文' if kind == 'note' else '视频'} {item_id}"


def normalize_douyin_item(item: dict[str, Any]) -> dict[str, Any] | None:
    url = (item.get("url") or "").strip().split("?")[0]
    raw_title = (item.get("title") or item.get("raw_text") or "").strip()
    raw_text = (item.get("raw_text") or "").strip()
    kind = "note" if "/note/" in url else "video"

    if not re.match(r"^https://www\.douyin\.com/(video|note)/\d+", url):
        return None
    if raw_title.startswith("热门") or raw_text.startswith("热门"):
        return None

    title = re.sub(r"\s+", " ", raw_title).strip()
    item_id = url.rstrip("/").rsplit("/", 1)[-1]
    if not title or re.fullmatch(r"(视频|图文)_\d+", title):
        title = _fallback_title(raw_text, kind, item_id)

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
      const anchors = Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="/note/"]'));
      for (const a of anchors) {
        const href = (a.href || '').split('?')[0];
        const match = href.match(/\/(video|note)\/(\d+)/);
        if (!match) continue;
        const kind = match[1];
        const itemId = match[2];
        if (seen.has(itemId)) continue;
        seen.add(itemId);
        const card = a.closest('li') || a.closest('[class*="item"]') || a.parentElement;
        const img = card ? card.querySelector('img') : null;
        const rawText = card ? (card.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 1000) : '';
        let title = '';
        // 标题优先取封面 alt；alt 是纯计数（「1.1万」）说明这张卡还没渲染出标题
        if (img && img.alt && !/^[\d.万亿]+$/.test(img.alt.trim())) {
          title = img.alt.trim();
        }
        if (!title) {
          // 退到卡片正文：列表卡片以播放量开头，剥掉计数后取前一段
          const text = (card ? (card.textContent || '') : '')
            .replace(/\s+/g, ' ').trim().replace(/^[\d.]+[万亿]?\s*/, '');
          if (text) title = text.slice(0, 120);
        }
        if (!title) title = (kind === 'note' ? '图文_' : '视频_') + itemId;
        results.push({
          item_id: itemId,
          kind,
          url: href,
          title,
          raw_text: rawText,
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


def _scroll_wait_ms(round_no: int) -> int:
    # 随轮次递增的随机等待：连续快滚最容易触发风控，越往下越慢。
    return min(
        DOUYIN_SCROLL_WAIT_BASE_MS
        + round_no * DOUYIN_SCROLL_WAIT_STEP_MS
        + random.randint(0, DOUYIN_SCROLL_WAIT_JITTER_MS),
        DOUYIN_SCROLL_WAIT_MAX_MS,
    )


def _scroll_state(page) -> dict[str, float] | None:
    """读滚动容器的几何位置；读不到/形状异常一律返回 None（下游按「未到底」处理）。"""
    try:
        value = page.evaluate(DOUYIN_SCROLL_STATE_JS)
    except BskCommandError:
        return None
    if not isinstance(value, dict):
        return None
    try:
        return {key: float(value.get(key) or 0) for key in ("top", "height", "client")}
    except (TypeError, ValueError):
        return None


def _at_scroll_bottom(state: dict[str, float] | None) -> bool:
    """容器明确可滚且已到末尾才算到底；容器不可滚/读不到状态都不算。"""
    if not state or state["height"] <= state["client"]:
        return False
    return state["top"] + state["client"] >= state["height"] - DOUYIN_BOTTOM_TOLERANCE_PX


def _wheel_with_retry(page, step: int, *, reinstall=None, attempts: int = 3) -> None:
    """滚动一次；被 strict 策略以 permission_denied 拒掉时重装停播器再试。

    实测：页面内容（懒加载卡片/预览视频）在轮次间仍在变化时，wheel 会被拒；
    等一拍、把页面重新压成静态后同一个滚动即可通过。非权限类错误不重试。
    """
    for attempt in range(attempts):
        try:
            page.mouse.wheel(0, step)
            return
        except BskCommandError as exc:
            if attempt + 1 >= attempts or "permission_denied" not in str(exc):
                raise
            if reinstall is not None:
                reinstall(page)
            page.wait_for_timeout(1000)


def fetch_douyin_favorites(
    env_path: str | None = None,
    *,
    session: BskRunnerSession | None = None,
    deep: bool = False,
    known_urls: list[str] | None = None,
) -> FetchBatch:
    # ponytail: env_path 仅为与 pipeline 的 fetcher(ENV_PATH, session=...) 调用姿势同形，
    # bsk 走用户已登录的浏览器，不需要 env 配置；若将来 bsk 需要 env 参数再接线。
    own_session = session is None
    if own_session:
        session = BskRunnerSession(start_url="about:blank")
        session.start()

    # 已知条目按 aweme id 比对（视频与图文同号段，按末段取）；深扫不看已知集合。
    known_ids = {
        tail.split("?")[0]
        for url in (known_urls or [])
        for tail in [re.split(r"/(?:video|note)/", str(url))[-1]]
        if re.search(r"/(?:video|note)/", str(url))
    }
    round_unknown = 0

    seen: dict[str, dict[str, Any]] = {}
    rounds = 0
    reached_bottom = False
    body_text = ""
    try:
        page = session.acquire_page()
        page.goto(DOUYIN_FAVORITE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        # 先停掉预览视频再滚：滚动会触发卡片自动播放，媒体源一变后续 wheel/evaluate 全被拒。
        _install_stabilizer(page)

        def _absorb(extracted: list[dict[str, Any]]) -> int:
            # 必须跨轮累积：收藏页虚拟列表会把滚出视口的卡片从 DOM 摘掉，每轮覆盖会丢条目。
            nonlocal round_unknown
            added = 0
            for item in extracted:
                key = str(item.get("item_id") or item.get("url") or "").strip()
                if not key or key in seen:
                    continue
                seen[key] = item
                added += 1
                if key not in known_ids:
                    round_unknown += 1
            return added

        _absorb(_extract_visible_items(page))
        # 逐轮下滚让懒加载条目进入 DOM；连续多轮零新增才认定到底/追上已知区域。
        limit = DOUYIN_DEEP_SCROLL_MAX_ROUNDS if deep else DOUYIN_SCROLL_ROUNDS
        step = DOUYIN_DEEP_SCROLL_STEP_PX if deep else DOUYIN_SCROLL_STEP_PX
        empty_rounds = 0
        stale_rounds = 0
        last_top: float | None = None
        for round_no in range(limit):
            _wheel_with_retry(page, step, reinstall=_install_stabilizer)
            page.wait_for_timeout(_scroll_wait_ms(round_no))
            rounds = round_no + 1
            round_unknown = 0
            added = _absorb(_extract_visible_items(page))
            # 「追平」的判据是连续多轮没有未入库条目，而不只是撞到几条已知：
            # 排序若被置顶/重排打乱，只撞 3 条已知就可能提前收工漏掉下面的新条目。
            # added==0（本轮无任何新面孔）与 round_unknown==0（本轮全是已入库）都算停滞。
            if added == 0 or round_unknown == 0:
                stale_rounds += 1
            else:
                stale_rounds = 0
            if not deep:
                if stale_rounds >= DOUYIN_STALE_ROUNDS:
                    reached_bottom = True
                    break
                continue
            if added:
                empty_rounds = 0
                continue
            empty_rounds += 1
            if empty_rounds < DOUYIN_SCROLL_EMPTY_ROUNDS:
                continue
            # 深度模式必须拿到「容器已滚到头」的几何证据；只凭零新增会把滚动卡住当成扫完，
            # 把仍在收藏里的条目整批误标「源内已移除」（2026-11-08 误标 780 条即此）。
            state = _scroll_state(page)
            if _at_scroll_bottom(state):
                reached_bottom = True
                break
            top = state["top"] if state else None
            moved = top is not None and (last_top is None or top > last_top + 1)
            last_top = top if top is not None else last_top
            if empty_rounds >= DOUYIN_SCROLL_STALL_ROUNDS and not moved:
                break  # 滚不动了：按未拿全收工，不判到底
        body_text = _body_text(page)
        try:
            page.close()
        except Exception:
            pass
    finally:
        if own_session:
            session.shutdown()

    login_wall = not seen and any(marker in body_text for marker in DOUYIN_LOGIN_WALL_MARKERS)
    # 只有深度全量滚到底、且确实拿到条目才算完整：增量模式即使滚到头也没扫全量；
    # 0 条更像是页面结构变了/没渲染出来，不能据此把库里所有收藏判成「已移除」。
    # ponytail: 底边靠「连续零新增」推断，若收藏页是无尽推荐流，深度扫描永远不完整（安全但功能失效）；
    # 升级路径 = 改走收藏接口 cursor 分页拿独立底边信号。
    complete = deep and reached_bottom and bool(seen)
    items = list(seen.values())
    # 调试产物的 ok 必须反映真实结论：登录墙时写 ok=false 并带失败原因，不能先写成功产物再抛错。
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_JSON_PATH.write_text(
        json.dumps(
            {
                "ok": not login_wall,
                "count": len(items),
                "items": items,
                "mode": "deep" if deep else "incremental",
                "rounds": rounds,
                "reached_bottom": reached_bottom,
                "complete": complete,
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
    seen_urls: set[str] = set()
    for item in items:
        normalized = normalize_douyin_item(item)
        if not normalized:
            continue
        url = normalized["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        result.append(normalized)
    return FetchBatch(items=result, complete=complete)

from __future__ import annotations

import json
import re
from typing import Any

from apps.laterhub.config import DEBUG_DIR
from connectors._shared.bsk_session import BskRunnerSession
from connectors._shared.common import FetchBatch


WEREAD_SHELF_URL = "https://weread.qq.com/web/shelf"
WEREAD_READER_PREFIX = "https://weread.qq.com/web/reader/"
# 书架接口按批取详情：整批 20 条；某批失败就对半拆分，拆到单条仍失败则跳过该 id。
WEREAD_SHELF_BATCH_SIZE = 20
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_JSON_PATH = DEBUG_DIR / "tmp_pw_weread_shelf.json"


def normalize_weread_item(item: dict[str, Any]) -> dict[str, Any] | None:
    reader_link = (item.get("readerLink") or "").strip()
    title = re.sub(r"\s+", " ", (item.get("title") or "").strip()).strip()

    if not reader_link.startswith(WEREAD_READER_PREFIX):
        return None
    if not title:
        return None

    return {
        "url": reader_link,
        "title": title,
        "source": "weread_shelf",
        "owner": "",
        "tags": None,
        # 书长期在书架上：抓取再命中不等于没读完，保留面板里的完成态。
        "keep_finished": True,
        "raw": item,
    }


# bsk 无请求拦截能力，改为在书架页内直接调同源接口：
# 页内 fetch 自动带登录 cookie，无需导出 token。
#
# ponytail: 依赖微信读书未公开的 /web/shelf/bookIds 与 /web/book/publicinfos；
# 接口或字段变更时会以「微信读书书架接口返回异常」可见地失败，届时按新接口更新本 JS。
_SHELF_JS = r"""
async (argsJson) => {
  const args = JSON.parse(argsJson);
  const base = location.origin;
  let idsResp;
  try {
    idsResp = await fetch(base + '/web/shelf/bookIds', {credentials: 'include', signal: AbortSignal.timeout(args.timeoutMs)});
  } catch (e) {
    return {error: '微信读书书架接口请求失败: ' + String(e)};
  }
  if (idsResp.status === 401 || idsResp.status === 403) {
    return {error: '微信读书登录态失效（bookIds 返回 ' + idsResp.status + '），请在日常 Chrome 重新登录微信读书'};
  }
  let idsData;
  try {
    idsData = await idsResp.json();
  } catch (e) {
    return {error: '微信读书书架接口返回非 JSON（可能登录态失效）'};
  }
  if (!idsResp.ok || !Array.isArray(idsData.data)) {
    return {error: '微信读书书架接口返回异常: ' + JSON.stringify(idsData).slice(0, 200)};
  }
  const ids = idsData.data.map(x => x && x.bookId).filter(Boolean);
  const known = new Set(args.knownBookIds || []);
  // 日常短抓取只取未入库的新书详情；深扫不看已知集合，必须全量才能判定移除。
  const targets = args.deep ? ids : ids.filter(id => !known.has(id));
  const books = new Map();
  const badIds = [];
  const fetchChunk = async (chunk) => {
    const r = await fetch(base + '/web/book/publicinfos', {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({bookIds: chunk}),
      signal: AbortSignal.timeout(args.timeoutMs),
    });
    if (!r.ok) return null;
    let d;
    try {
      d = await r.json();
    } catch (e) {
      return null;
    }
    return Array.isArray(d.data) ? d.data : null;
  };
  // 一个坏 bookId（实测 'mpbook'，errCode -2003）会让整批报错，故失败即对半拆分；
  // 拆到单条仍失败只记入 badIds，绝不因一个坏 id 丢掉整批。
  const grab = async (chunk) => {
    let d = null;
    try {
      d = await fetchChunk(chunk);
    } catch (e) {
      d = null;
    }
    if (d) {
      for (const b of d) if (b && b.bookId) books.set(b.bookId, b);
      return;
    }
    if (chunk.length === 1) {
      badIds.push(chunk[0]);
      return;
    }
    const mid = Math.ceil(chunk.length / 2);
    await grab(chunk.slice(0, mid));
    await grab(chunk.slice(mid));
  };
  for (let i = 0; i < targets.length; i += args.batchSize) await grab(targets.slice(i, i + args.batchSize));
  return {ids: ids.length, targets: targets.length, books: Array.from(books.values()), badIds};
}
"""


def _known_book_ids(known_urls: list[str] | None) -> list[str]:
    """从已入库 URL 提取 bookId（/web/reader/{bookId}），供日常短抓取跳过已入库书籍。"""
    found: list[str] = []
    for url in known_urls or []:
        match = re.search(r"/web/reader/([0-9A-Za-z]+)", str(url))
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def fetch_weread_shelf(
    env_path: str | None = None,
    *,
    session: BskRunnerSession | None = None,
    deep: bool = False,
    known_urls: list[str] | None = None,
) -> FetchBatch:
    # ponytail: env_path 仅为与 pipeline 的 fetcher(ENV_PATH, session=...) 调用姿势同形，
    # bsk 复用用户日常 Chrome 的登录态，不需要 env 配置。
    own_session = session is None
    if own_session:
        session = BskRunnerSession(start_url="about:blank")
        session.start()

    raw_items: list[dict[str, Any]] = []
    bad_ids: list[str] = []
    result: list[dict[str, Any]] = []
    error_message = ""
    page = None
    try:
        page = session.acquire_page()
        page.goto(WEREAD_SHELF_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        payload = page.evaluate(
            _SHELF_JS,
            json.dumps(
                {
                    "batchSize": WEREAD_SHELF_BATCH_SIZE,
                    "timeoutMs": 20000,
                    "deep": bool(deep),
                    "knownBookIds": _known_book_ids(known_urls),
                }
            ),
            timeout=120,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"微信读书书架接口返回异常类型: {type(payload).__name__}")
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        books = payload.get("books")
        if not isinstance(books, list):
            raise RuntimeError("微信读书书架接口返回异常（books 非数组）")
        bad_ids = [str(x) for x in (payload.get("badIds") or [])]
        raw_items = [item for item in books if isinstance(item, dict)]

        seen: set[str] = set()
        for item in raw_items:
            normalized = normalize_weread_item(item)
            if not normalized:
                continue
            url = normalized["url"]
            if url in seen:
                continue
            seen.add(url)
            result.append(normalized)
    except Exception as exc:  # noqa: BLE001
        if not error_message:
            error_message = str(exc) if isinstance(exc, RuntimeError) else f"微信读书书架抓取失败: {exc}"
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if own_session:
            session.shutdown()

    # 调试产物的 ok 必须反映真实结论：失败时写 ok=false 并带原因，不能先写成功再抛错。
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_JSON_PATH.write_text(
        json.dumps(
            {
                "ok": not error_message,
                "count": len(result),
                "raw_count": len(raw_items),
                "items": result,
                "bad_ids": bad_ids,
                **({"error": error_message} if error_message else {}),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if error_message:
        raise RuntimeError(error_message)
    # 日常短抓取永远不完整；深扫只有「没有坏 id」才算拿全：坏 id 无法确认对应书是否还在架，
    # 宁可不做移除判定，也不能把仍在架上的书误标成已读完。
    return FetchBatch(items=result, complete=bool(deep and not bad_ids))

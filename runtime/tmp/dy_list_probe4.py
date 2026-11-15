"""只读探测 3：拿收藏列表请求的完整 URL（含签名），并测试页内重放能否读到 aweme_list。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from connectors._shared.bsk_session import BskRunnerSession
from connectors.douyin.favorites import DOUYIN_STABILIZE_MEDIA_JS

FAV_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"

REPLAY_JS = r"""
async (url) => {
  try {
    const resp = await fetch(url, { credentials: 'include', signal: AbortSignal.timeout(20000) });
    const text = await resp.text();
    let parsed = null;
    try { parsed = JSON.parse(text); } catch (e) {}
    const list = parsed ? (parsed.aweme_list || []) : [];
    return {
      status: resp.status,
      bytes: text.length,
      head: text.slice(0, 200),
      has_more: parsed ? parsed.has_more : null,
      cursor: parsed ? parsed.cursor : null,
      count: list.length,
      first_keys: list[0] ? Object.keys(list[0]).slice(0, 30) : [],
      first: list[0]
        ? {
            aweme_id: list[0].aweme_id,
            desc: (list[0].desc || '').slice(0, 50),
            author: (list[0].author || {}).nickname,
            collect_time: list[0].collect_time || null,
            play: ((list[0].video || {}).play_addr || {}).url_list || [],
            subs: ((list[0].video || {}).subtitle_infos || []).length,
            images: (list[0].images || []).length,
          }
        : null,
    };
  } catch (e) {
    return { error: String(e) };
  }
}
"""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    session = BskRunnerSession(start_url="about:blank")
    session.start()
    page = session.acquire_page()
    report: dict = {}
    try:
        page.goto(FAV_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        page.evaluate(DOUYIN_STABILIZE_MEDIA_JS)
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(3000)

        entries = page.network_entries(limit=400, max_text_chars=4096)
        report["entry_count"] = len(entries)
        hit = next((e for e in entries if "listcollection" in str(e.get("url") or "")), None)
        if hit:
            report["keys"] = sorted(hit.keys())
            report["entry"] = {k: (str(v)[:120] if not isinstance(v, (int, float, bool, type(None))) else v) for k, v in hit.items()}
            report["url_len"] = len(str(hit.get("url") or ""))
            report["replay"] = page.evaluate(REPLAY_JS, str(hit.get("url")))
        else:
            report["error"] = "网络缓冲里没有 listcollection 请求"
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        session.shutdown()

    out = Path(__file__).with_name("dy_list_probe4.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2)[:8000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

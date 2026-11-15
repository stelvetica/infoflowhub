"""只读探测：收藏页的标签页/收藏夹结构、账号收藏总量线索。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from connectors._shared.bsk_session import BskRunnerSession
from connectors.douyin.favorites import DOUYIN_STABILIZE_MEDIA_JS

FAV_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"

INFO_JS = r"""
() => {
  const bodyText = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 1200);
  const tabs = Array.from(document.querySelectorAll('[role="tab"], [class*="tab"]'))
    .map((n) => (n.textContent || '').trim()).filter((t) => t && t.length < 20);
  const folderish = Array.from(document.querySelectorAll('*'))
    .filter((n) => !n.children.length)
    .map((n) => (n.textContent || '').trim())
    .filter((t) => /收藏夹|合集|全部收藏|我的收藏/.test(t) && t.length < 20);
  const anchors = document.querySelectorAll('a[href*="/video/"]').length;
  const counts = Array.from(document.querySelectorAll('*'))
    .filter((n) => !n.children.length)
    .map((n) => (n.textContent || '').trim())
    .filter((t) => /^\d+$/.test(t) && t.length <= 6).slice(0, 15);
  return {
    anchors,
    tabs: Array.from(new Set(tabs)).slice(0, 15),
    folderish: Array.from(new Set(folderish)).slice(0, 15),
    numbers: counts,
    bodyText,
  };
}
"""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    session = BskRunnerSession(start_url="about:blank")
    session.start()
    page = session.acquire_page()
    report = {}
    try:
        page.goto(FAV_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)
        page.evaluate(DOUYIN_STABILIZE_MEDIA_JS)
        report = page.evaluate(INFO_JS)
    except Exception as exc:
        report = {"error": str(exc)[:200]}
    finally:
        session.shutdown()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

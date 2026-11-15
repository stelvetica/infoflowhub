"""只读探测 2：收藏页滚动后，从 bsk 网络缓冲里找出当前的列表接口路径与响应结构。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from connectors._shared.bsk_session import BskRunnerSession
from connectors.douyin.favorites import DOUYIN_STABILIZE_MEDIA_JS

FAV_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    session = BskRunnerSession(start_url="about:blank")
    session.start()
    page = session.acquire_page()
    report: dict = {"entries": [], "perf": []}
    try:
        page.goto(FAV_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        page.evaluate(DOUYIN_STABILIZE_MEDIA_JS)
        for _ in range(3):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(2500)

        seen = set()
        for entry in page.network_entries(limit=400, max_text_chars=2000):
            url = str(entry.get("url") or "")
            if "aweme" not in url:
                continue
            path = url.split("?")[0]
            key = (path, entry.get("status"))
            if key in seen:
                continue
            seen.add(key)
            report["entries"].append(
                {
                    "path": path,
                    "status": entry.get("status"),
                    "len": len(str(entry.get("text") or "")),
                    "head": str(entry.get("text") or "")[:300],
                }
            )

        try:
            report["perf"] = page.evaluate(
                """() => [...new Set(performance.getEntriesByType('resource')
                     .map(e => e.name).filter(u => u.includes('aweme'))
                     .map(u => u.split('?')[0]))].slice(0, 30)"""
            )
        except Exception as exc:  # noqa: BLE001
            report["perf_error"] = str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        session.shutdown()

    out = Path(__file__).with_name("dy_list_probe3.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

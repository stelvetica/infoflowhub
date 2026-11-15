"""只读探测 5：视频页是否真的请求了字幕文件（自带字幕的网络证据）。

不依赖用户脚本改动：只看 bsk 网络缓冲里有没有 srt/vtt/subtitle 类请求。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from connectors._shared.bsk_session import BskRunnerSession

VIDEOS = [
    "https://www.douyin.com/video/7705709382827555327",
    "https://www.douyin.com/video/7703823523478883610",
    "https://www.douyin.com/video/7676816287078616329",
]
PATTERNS = ("srt", "vtt", "subtitle", "caption", "sub_", "/web/sub")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    session = BskRunnerSession(start_url="about:blank")
    session.start()
    page = session.acquire_page()
    report: dict = {"videos": []}
    try:
        for url in VIDEOS:
            item: dict = {"url": url}
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(9000)
                entries = page.network_entries(limit=400, max_text_chars=800)
                urls = [str(e.get("url") or "") for e in entries]
                item["total_entries"] = len(urls)
                item["hits"] = [
                    {"url": u.split("?")[0], "query_len": len(u), "status": status}
                    for u, status in [
                        (str(e.get("url") or ""), e.get("status"))
                        for e in entries
                        if any(p in str(e.get("url") or "").lower() for p in PATTERNS)
                    ]
                ][:10]
                item["media_count"] = sum(1 for u in urls if "douyinvod.com" in u)
            except Exception as exc:  # noqa: BLE001
                item["error"] = f"{type(exc).__name__}: {exc}"[:200]
            report["videos"].append(item)
            page.wait_for_timeout(1500)
    finally:
        session.shutdown()

    out = Path(__file__).with_name("dy_subtitle_probe2.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

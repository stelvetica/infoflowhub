"""只读探测 4：抖音视频页 HTML 里有没有 subtitle_infos（自带字幕），以及字幕 URL 长什么样。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from connectors._shared.bsk_session import BskRunnerSession

VIDEO_URL = "https://www.douyin.com/video/7705709382827555327"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    session = BskRunnerSession(start_url="about:blank")
    session.start()
    page = session.acquire_page()
    report: dict = {}
    try:
        page.goto(VIDEO_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        text = page.content()
        report["html_len"] = len(text)
        report["subtitle_infos_count"] = text.count("subtitle_infos")
        report["caption_count"] = text.count("captions")
        report["marker_present"] = "ifh-douyin-media" in text
        hits = [m.start() for m in re.finditer("subtitle_infos", text)][:3]
        report["snippets"] = [text[max(0, i - 120) : i + 420] for i in hits]
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        session.shutdown()

    out = Path(__file__).with_name("dy_subtitle_probe.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "snippets"}, ensure_ascii=False, indent=2))
    for s in report.get("snippets", []):
        print("----")
        print(s[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

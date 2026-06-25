from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from apps.subscriptions.rss_config import load_sources
from apps.subscriptions.runtime_health import run_source_fetch


def main() -> int:
    source = next((item for item in load_sources() if item.get("id") == "alphapai"), None)
    if not source:
        print("未找到 alphapai 订阅源配置")
        return 1

    outcome = run_source_fetch([source], timeout=120)
    if outcome.fatal_error:
        print(json.dumps({"ok": False, "error": outcome.fatal_error}, ensure_ascii=False, indent=2))
        return 1

    result = outcome.results[0] if outcome.results else None
    inserted = int(outcome.inserted_by_source.get(source["id"], 0))
    if result is None:
        print(json.dumps({"ok": False, "error": "no result"}, ensure_ascii=False, indent=2))
        return 1

    payload = {
        "ok": result.ok,
        "status": result.status,
        "source_id": result.source_id,
        "source_name": result.source_name,
        "error": result.error,
        "entry_count": len(result.entries),
        "inserted": inserted,
        "titles": [item.title for item in result.entries[:10]],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

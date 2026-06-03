"""Update Chrome bookmark with latest InfoFlowHub tunnel URL.
Chrome Sync will push to all synced devices.
"""

import json
import hashlib
import sys
from datetime import datetime, timezone


def update_chrome_bookmark(bookmarks_path, url_str):
    url_str = url_str.rstrip("/")  # normalize

    with open(bookmarks_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    bar = data["roots"]["bookmark_bar"]
    meta = {
        "power_bookmark_meta": ""
    }
    now = str(int(datetime.now(timezone.utc).timestamp() * 1_000_000))

    # Find existing
    found = None
    for c in bar.get("children", []):
        if c.get("name") == "InfoFlowHub":
            found = c
            break

    if found:
        old = found.get("url", "")
        if old == url_str:
            return "UNCHANGED"
        found["url"] = url_str
        result = f"UPDATED|{old}|{url_str}"
    else:
        # Insert at front of bookmark bar
        new_bm = {
            "date_added": now,
            "date_last_used": "0",
            "guid": "infoflowhub-auto-tunnel-001",
            "id": "9999",
            "meta_info": meta,
            "name": "InfoFlowHub",
            "type": "url",
            "url": url_str,
        }
        bar["children"].insert(0, new_bm)
        result = f"CREATED||{url_str}"

    # Recalculate checksum
    if "checksum" in data:
        del data["checksum"]
    raw = json.dumps(data, ensure_ascii=False, indent=3)
    checksum = hashlib.md5(raw.encode("utf-8")).hexdigest()
    data["checksum"] = checksum

    with open(bookmarks_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=3)

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: update_chrome_bookmark.py <tunnel_url>")
        sys.exit(1)

    path = r"C:\Users\TB14Plus\AppData\Local\Google\Chrome\User Data\Default\Bookmarks"
    try:
        result = update_chrome_bookmark(path, sys.argv[1])
        print(result)
    except Exception as e:
        print(f"ERROR|{e}")
        sys.exit(1)

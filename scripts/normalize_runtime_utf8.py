from __future__ import annotations

import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from apps.subscriptions.config import load_sources
from apps.subscriptions.source_ids import canonicalize_source_id
from infra.text_normalizer import normalize_utf8_text
from infra.utf8_json import dump_json_utf8, load_json_utf8
from web.services.views import load_health

RUNTIME_DIR = BASE_DIR / "runtime"
HEALTH_DIR = RUNTIME_DIR / "health"


def normalize_wechat_auth_file(path: Path) -> None:
    payload = load_json_utf8(path, default={})
    if not isinstance(payload, dict):
        payload = {}
    payload["nickname"] = normalize_utf8_text(payload.get("nickname"))
    dump_json_utf8(path, payload)


def normalize_automation_runtime() -> None:
    path = HEALTH_DIR / "automation_runtime.json"
    payload = load_json_utf8(path, default={"slots": {}})
    if not isinstance(payload, dict):
        payload = {"slots": {}}
    slots = payload.get("slots")
    if not isinstance(slots, dict):
        payload["slots"] = {}
        dump_json_utf8(path, payload)
        return
    for slot in slots.values():
        if isinstance(slot, dict) and "label" in slot:
            slot["label"] = normalize_utf8_text(slot.get("label"))
    dump_json_utf8(path, payload)


def normalize_source_health() -> None:
    path = HEALTH_DIR / "subscriptions_source_health.json"
    payload = load_health()
    source_name_map = {
        canonicalize_source_id(str(item.get("id") or "")): normalize_utf8_text(item.get("name") or "")
        for item in load_sources()
    }
    sources = payload.setdefault("sources", {})
    for source_id, item in list(sources.items()):
        if not isinstance(item, dict):
            sources[source_id] = {
                "last_checked_at": "",
                "last_success_at": "",
                "last_failed_at": "",
                "last_error": "",
            }
            continue
        canonical_id = canonicalize_source_id(source_id)
        if canonical_id in source_name_map:
            item["source_name"] = source_name_map[canonical_id]
        elif item.get("source_name"):
            item["source_name"] = normalize_utf8_text(item.get("source_name"))
        item["last_error"] = normalize_utf8_text(item.get("last_error"))
    dump_json_utf8(path, payload)


def main() -> None:
    normalize_wechat_auth_file(RUNTIME_DIR / "wechat_auth.json")
    normalize_wechat_auth_file(RUNTIME_DIR / "auth" / "wechat_mp_main.json")
    normalize_automation_runtime()
    normalize_source_health()


if __name__ == "__main__":
    main()

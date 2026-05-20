from __future__ import annotations

from typing import Iterable

from infra.text_normalizer import normalize_utf8_text

SOURCE_ID_ALIASES: dict[str, str] = {
    "wechat-kindle-jingxuanjun": "wechat-jingxuanjun",
}


def canonicalize_source_id(source_id: str) -> str:
    clean_id = normalize_utf8_text(source_id)
    return SOURCE_ID_ALIASES.get(clean_id, clean_id)


def legacy_source_ids(canonical_source_id: str) -> list[str]:
    clean_id = normalize_utf8_text(canonical_source_id)
    return [legacy_id for legacy_id, target_id in SOURCE_ID_ALIASES.items() if target_id == clean_id]


def source_id_family(source_id: str) -> list[str]:
    canonical_id = canonicalize_source_id(source_id)
    return [canonical_id, *legacy_source_ids(canonical_id)]


def merge_source_health_rows(rows: Iterable[tuple[str, dict]]) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for raw_source_id, raw_payload in rows:
        source_id = canonicalize_source_id(raw_source_id)
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        current = merged.setdefault(
            source_id,
            {
                "last_checked_at": "",
                "last_success_at": "",
                "last_failed_at": "",
                "last_error": "",
            },
        )
        last_checked_at = str(payload.get("last_checked_at") or "")
        last_success_at = str(payload.get("last_success_at") or "")
        last_failed_at = str(payload.get("last_failed_at") or "")
        last_error = normalize_utf8_text(payload.get("last_error") or "")
        if last_checked_at > current["last_checked_at"]:
            current["last_checked_at"] = last_checked_at
        if last_success_at > current["last_success_at"]:
            current["last_success_at"] = last_success_at
        if last_failed_at > current["last_failed_at"]:
            current["last_failed_at"] = last_failed_at
        if last_error and last_failed_at >= current["last_failed_at"]:
            current["last_error"] = last_error
        elif not current["last_error"] and last_error:
            current["last_error"] = last_error
        if "source_name" in payload:
            source_name = normalize_utf8_text(payload.get("source_name") or "")
            if source_name:
                current["source_name"] = source_name
        if "feed_url" in payload:
            feed_url = str(payload.get("feed_url") or "")
            if feed_url:
                current["feed_url"] = feed_url
    return merged

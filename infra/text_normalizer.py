from __future__ import annotations

from typing import Any


MOJIBAKE_HINTS = (
    "鍒",
    "姣",
    "璁",
    "绋",
    "鎽",
    "璺",
    "纭",
    "宸",
    "闃",
    "閾",
    "鑲",
    "鏄",
    "鐮",
    "姹",
    "榛",
    "鐧",
    "鏈",
    "缁",
    "鍏",
    "绔",
)


def clean_text(value: object) -> str:
    return str(value or "").strip()


def looks_like_mojibake(text: str) -> bool:
    candidate = clean_text(text)
    return bool(candidate) and any(token in candidate for token in MOJIBAKE_HINTS)


def repair_mojibake_text(value: object) -> str:
    text = clean_text(value)
    if not text or not looks_like_mojibake(text):
        return text
    for source_encoding in ("gb18030", "gbk", "latin1", "cp1252"):
        try:
            repaired = text.encode(source_encoding, "ignore").decode("utf-8", "ignore").strip()
        except Exception:
            continue
        if repaired and repaired != text and not looks_like_mojibake(repaired):
            return repaired
    return text


def normalize_utf8_text(value: object) -> str:
    return repair_mojibake_text(value)


def normalize_utf8_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {normalize_utf8_text(key): normalize_utf8_obj(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_utf8_obj(item) for item in value]
    if isinstance(value, str):
        return normalize_utf8_text(value)
    return value

from __future__ import annotations

from typing import Any


MOJIBAKE_HINTS = (
    "閸",
    "濮",
    "鐠",
    "缁",
    "閹",
    "绾",
    "瀹",
    "闂",
    "闁",
    "閼",
    "閺",
    "閻",
    "姒",
    "缂",
    "鍏",
    "鏈",
    "鐧",
    "榛",
    "宸",
    "绋",
    "鍒",
    "鎶",
    "璇",
    "鏃",
    "杩",
    "鎴",
    "浠",
    "鏍",
    "寰",
    "娌",
    "鎼",
    "姝",
    "Ã",
    "Â",
    "ð",
    "æ",
    "ç",
    "è",
    "é",
    "å",
    "ä",
    "ï",
)


def clean_text(value: object) -> str:
    return str(value or "").strip()


def looks_like_mojibake(text: str) -> bool:
    candidate = clean_text(text)
    if not candidate:
        return False
    if any(token in candidate for token in MOJIBAKE_HINTS):
        return True
    suspicious_count = sum(candidate.count(token) for token in ("Ã", "Â", "æ", "ç", "è", "é", "å", "ä", "ï", "ð"))
    has_cjk = any("\u4e00" <= char <= "\u9fff" for char in candidate)
    return suspicious_count >= 2 and not has_cjk


def repair_mojibake_text(value: object) -> str:
    text = clean_text(value)
    if not text or not looks_like_mojibake(text):
        return text
    for source_encoding in ("latin1", "cp1252", "gb18030", "gbk"):
        try:
            repaired = text.encode(source_encoding, "ignore").decode("utf-8", "ignore").strip()
        except Exception:
            continue
        if repaired and repaired != text and not looks_like_mojibake(repaired):
            return repaired
    return text


def normalize_utf8_text(value: object) -> str:
    return repair_mojibake_text(value)


def normalize_text_lines(value: object) -> str:
    text = normalize_utf8_text(value)
    if not text:
        return ""
    return "\n".join(normalize_utf8_text(line) for line in text.splitlines())


def normalize_utf8_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {normalize_utf8_text(key): normalize_utf8_obj(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_utf8_obj(item) for item in value]
    if isinstance(value, str):
        return normalize_utf8_text(value)
    return value


def normalize_utf8_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_utf8_text(value)
    return value

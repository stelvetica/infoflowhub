from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parents[3]
ENV_PATH = BASE_DIR / ".env"


def _load_env() -> None:
    if load_dotenv is not None and ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=False)


def get_bilibili_cookie() -> str:
    _load_env()
    cookie = os.getenv("BILIBILI_COOKIE", "").strip()
    sessdata = os.getenv("BILIBILI_SESSDATA", "").strip()
    bili_jct = os.getenv("BILIBILI_BILI_JCT", "").strip()
    dedeuserid = os.getenv("BILIBILI_DEDEUSERID", "").strip()
    if not cookie and sessdata:
        pieces = [f"SESSDATA={sessdata}"]
        if bili_jct:
            pieces.append(f"bili_jct={bili_jct}")
        if dedeuserid:
            pieces.append(f"DedeUserID={dedeuserid}")
        cookie = "; ".join(pieces)
    return cookie


def validate_bilibili_auth() -> dict[str, object]:
    cookie = get_bilibili_cookie()
    if not cookie:
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未配置",
            "hint": "请在 .env 中配置 BILIBILI_COOKIE，或至少配置 BILIBILI_SESSDATA。",
        }
    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用",
        "hint": "当前 B 站主账号登录态已配置，稍后看与动态抓取会统一复用。",
    }


def get_bilibili_headers() -> dict[str, str]:
    cookie = get_bilibili_cookie()
    if not cookie:
        raise ValueError("缺少 B 站登录态。请先配置 bilibili_main。")
    return {"Cookie": cookie}

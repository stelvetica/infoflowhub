from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from infra.text_normalizer import clean_text, normalize_utf8_text
from infra.utf8_json import dump_json_utf8, load_json_utf8


BASE_DIR = Path(__file__).resolve().parents[3]
ENV_PATH = BASE_DIR / ".env"
RUNTIME_DIR = BASE_DIR / "runtime"
AUTH_DIR = RUNTIME_DIR / "auth"
BILIBILI_AUTH_PATH = AUTH_DIR / "bilibili_main.json"
WEB_LOG_PATH = RUNTIME_DIR / "web.log"

# B站动态接口只需这 3 个字段（SESSDATA 登录态 / bili_jct CSRF / DedeUserID 用户标识）
BILIBILI_COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID")
BILIBILI_HOME_URL = "https://www.bilibili.com/"
BILIBILI_NAV_API = "https://api.bilibili.com/x/web-interface/nav"


def _load_env() -> None:
    if load_dotenv is not None and ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=False)


def _log(message: str) -> None:
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        WEB_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with WEB_LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(f"[{timestamp}] [bilibili_auth] {normalize_utf8_text(message)}\n")
    except Exception:
        return


def _cookie_from_env() -> str:
    """从 .env 拼 cookie（回退兜底，向后兼容旧配置）。"""
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


def _load_canonical_cookie() -> str:
    """读持久化文件里的 cookie（对标 wechat 的 canonical 存储）。"""
    try:
        payload = load_json_utf8(BILIBILI_AUTH_PATH, default=None)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return clean_text(payload.get("cookie"))


def _persist_cookie(cookie: str, nickname: str = "", mid: str = "") -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    dump_json_utf8(
        BILIBILI_AUTH_PATH,
        {
            "cookie": cookie,
            "nickname": normalize_utf8_text(nickname),
            "mid": clean_text(mid),
            "synced_at": int(time.time() * 1000),
        },
    )


def get_bilibili_cookie() -> str:
    """SSOT：优先读持久化文件，回退 .env。"""
    canonical = _load_canonical_cookie()
    if canonical:
        return canonical
    _load_env()
    return _cookie_from_env()


def get_bilibili_headers() -> dict[str, str]:
    cookie = get_bilibili_cookie()
    if not cookie:
        raise ValueError("缺少 B 站登录态。请先在设置界面点击「同步登录态」，或在 .env 配置 BILIBILI_COOKIE。")
    return {"Cookie": cookie}


def _query_nav(cookie: str) -> dict[str, Any]:
    """调 nav 接口探测登录态（绕过系统代理，B站是国内站点不该走代理）。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        ),
        "Cookie": cookie,
        "Referer": "https://www.bilibili.com/",
    }
    try:
        response = requests.get(
            BILIBILI_NAV_API,
            headers=headers,
            timeout=8,
            proxies={"http": "", "https": ""},  # 绕过系统代理（127.0.0.1:7892 等可能未开）
        )
        data = response.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def export_bilibili_cookie_from_chrome() -> str:
    """从共享 auth profile 读出 B站 cookie，持久化到 bilibili_main.json。

    前置：需先运行 `python scripts/login_profiles.py bilibili` 扫码登录 B站，
    登录态存入共享 auth profile（与抖音/X 同机制，Playwright 管理可读明文）。
    本函数启动 headless Chrome 挂载该 profile，context.cookies() 读出 SESSDATA
    等，无需关本机 Chrome（共享 profile 是独立目录，无锁冲突）。
    """
    from connectors._shared.chrome_runner import _resolve_default_browser_executable
    from connectors._shared.common import USER_AGENT
    from connectors.auth.providers.browser_profiles import AUTH_PROFILE_DIR
    from playwright.sync_api import sync_playwright

    if not AUTH_PROFILE_DIR.exists():
        raise RuntimeError(
            "共享 auth profile 不存在，请先运行 `python scripts/login_profiles.py bilibili` 扫码登录 B站。"
        )

    _, chrome_executable = _resolve_default_browser_executable()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(AUTH_PROFILE_DIR),
            executable_path=chrome_executable,
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--no-first-run",
                f"--user-agent={USER_AGENT}",
            ],
            ignore_default_args=["--enable-automation"],
        )
        try:
            page = ctx.new_page()
            page.goto(BILIBILI_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            cookies = ctx.cookies()
        finally:
            ctx.close()

    cookie_map = {}
    for item in cookies:
        host = str(item.get("domain") or "")
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name in BILIBILI_COOKIE_KEYS and ".bilibili.com" in host and value:
            cookie_map[name] = value

    missing = [k for k in BILIBILI_COOKIE_KEYS if k not in cookie_map]
    if missing:
        raise RuntimeError(
            f"共享 auth profile 未读到完整 B站登录态，缺失 {missing}。"
            f"请运行 `python scripts/login_profiles.py bilibili` 扫码登录 B站后重试。"
        )

    cookie = "; ".join(f"{k}={cookie_map[k]}" for k in BILIBILI_COOKIE_KEYS)

    nav = _query_nav(cookie)
    data = nav.get("data") or {}
    nickname = str(data.get("uname") or "")
    mid = str(data.get("mid") or "")

    _persist_cookie(cookie, nickname=nickname, mid=mid)
    _log(f"已从常用 Chrome 同步 B站登录态（账号={nickname or '未知'} mid={mid or '未知'}）。")
    return cookie


def export_bilibili_cookie_from_live_chrome() -> str:
    """用 bb-browser 从『正在运行的真实 Chrome』实时读 B站 cookie 并持久化。

    bb-browser 直连用户日常 Chrome（复用它已登录的 B站会话），无需扫码、无需
    复制 profile、无需关 Chrome。与复制方案一致仍需关闭 ABE。

    返回拼好的 cookie 串；B站登录态三字段缺失或 nav 校验失败时抛 RuntimeError。
    """
    from connectors.auth.providers.bb_browser import export_cookies_from_live_chrome

    cookies = export_cookies_from_live_chrome(
        (".bilibili.com",),
        open_url=BILIBILI_HOME_URL,
    )
    cookie_map = {}
    for item in cookies:
        host = str(item.get("domain") or "")
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name in BILIBILI_COOKIE_KEYS and ".bilibili.com" in host and value:
            cookie_map[name] = value

    missing = [k for k in BILIBILI_COOKIE_KEYS if k not in cookie_map]
    if missing:
        raise RuntimeError(
            f"真实 Chrome 未读到完整 B站登录态，缺失 {missing}。"
            f"请确认日常 Chrome 已登录 B站（且已关闭 ABE）后重试。"
        )

    cookie = "; ".join(f"{k}={cookie_map[k]}" for k in BILIBILI_COOKIE_KEYS)

    nav = _query_nav(cookie)
    data = nav.get("data") or {}
    nickname = str(data.get("uname") or "")
    mid = str(data.get("mid") or "")

    _persist_cookie(cookie, nickname=nickname, mid=mid)
    _log(f"已从真实 Chrome 同步 B站登录态（账号={nickname or '未知'} mid={mid or '未知'}）。")
    return cookie


def validate_bilibili_auth() -> dict[str, object]:
    cookie = get_bilibili_cookie()
    if not cookie:
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未配置",
            "hint": "请在设置界面点击「同步登录态」从常用 Chrome 导入，或在 .env 配置 BILIBILI_COOKIE。",
        }

    nav = _query_nav(cookie)
    data = nav.get("data") or {}
    is_login = bool(data.get("isLogin"))
    nickname = str(data.get("uname") or "")

    if not is_login:
        _log("B站登录态已失效（nav 返回未登录），需重新同步。")
        return {
            "is_available": True,
            "status_level": "danger",
            "status_text": "已失效",
            "hint": "B站登录态已过期，请在设置界面点击「同步登录态」重新从常用 Chrome 导入。",
            "is_expired": True,
            "nickname": nickname,
        }

    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用" + (f"（{nickname}）" if nickname else ""),
        "hint": "B站登录态可用；失效时点击「同步登录态」从常用 Chrome 重新导入即可。",
        "is_expired": False,
        "nickname": nickname,
    }

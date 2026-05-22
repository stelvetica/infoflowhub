from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

import httpx

from connectors.auth.providers.wechat import (
    get_wechat_credentials,
    log_wechat_auth_event,
    save_wechat_credentials,
)
from infra.text_normalizer import normalize_utf8_text


MP_BASE_URL = "https://mp.weixin.qq.com"
QR_ENDPOINT = f"{MP_BASE_URL}/cgi-bin/scanloginqrcode"
BIZ_LOGIN_ENDPOINT = f"{MP_BASE_URL}/cgi-bin/bizlogin"
WECHAT_HOME_URL = f"{MP_BASE_URL}/cgi-bin/home"
WECHAT_SEARCH_BIZ_URL = f"{MP_BASE_URL}/cgi-bin/searchbiz"
WECHAT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def new_session_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _cookie_header_to_dict(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies


def _merge_cookie_headers(existing_header: str, response: httpx.Response) -> str:
    cookies = _cookie_header_to_dict(existing_header)
    for cookie in response.cookies.jar:
        cookies[cookie.name] = cookie.value
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def _merge_set_cookie_values(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for value in group:
            if value and value not in merged:
                merged.append(value)
    return merged


def _filter_set_cookie_headers(response: httpx.Response, is_https: bool) -> list[str]:
    values = response.headers.get_list("set-cookie")
    if is_https:
        return values
    return [value.replace("; Secure", "") for value in values]


def _extract_cookie_expire_time_ms(set_cookie_headers: list[str]) -> int:
    expire_candidates: list[int] = []
    now_ms = int(time.time() * 1000)
    for raw in set_cookie_headers:
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            continue
        for morsel in cookie.values():
            expires = str(morsel["expires"] or "").strip()
            if not expires:
                continue
            try:
                dt = parsedate_to_datetime(expires)
            except Exception:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            expire_ms = int(dt.timestamp() * 1000)
            if expire_ms <= now_ms:
                continue
            expire_candidates.append(expire_ms)
    return min(expire_candidates) if expire_candidates else 0


def _resolve_expire_time_ms(set_cookie_headers: list[str]) -> int:
    expire_time = _extract_cookie_expire_time_ms(set_cookie_headers)
    return expire_time if expire_time > 0 else int(time.time() * 1000)


def _contains_expired_session_cookies(set_cookie_headers: list[str]) -> bool:
    for raw in set_cookie_headers:
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            continue
        for morsel in cookie.values():
            if str(morsel.value or "").strip().upper() == "EXPIRED":
                return True
    return False


def _format_expire_time(expire_time_ms: int) -> str:
    if expire_time_ms <= 0:
        return ""
    return datetime.fromtimestamp(expire_time_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


async def proxy_wechat_request(
    *,
    url: str,
    cookie_header: str,
    method: str = "GET",
    params: dict | None = None,
    data: dict | None = None,
) -> httpx.Response:
    headers = {
        "User-Agent": WECHAT_USER_AGENT,
        "Referer": "https://mp.weixin.qq.com/",
        "Origin": "https://mp.weixin.qq.com",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": cookie_header,
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        if method.upper() == "POST":
            return await client.post(url, params=params, data=data, headers=headers)
        return await client.get(url, params=params, headers=headers)


async def _probe_api_session(
    *,
    token: str,
    cookie_header: str,
    query: str = "公众号",
) -> tuple[bool, str, list[str], dict | str]:
    response = await proxy_wechat_request(
        url=WECHAT_SEARCH_BIZ_URL,
        cookie_header=cookie_header,
        params={
            "action": "search_biz",
            "token": token,
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
            "random": time.time(),
            "query": query,
            "begin": 0,
            "count": 1,
        },
    )
    merged_cookie = _merge_cookie_headers(cookie_header, response)
    set_cookie_headers = _filter_set_cookie_headers(response, is_https=False)
    try:
        payload = response.json()
    except Exception:
        return False, merged_cookie, set_cookie_headers, response.text
    ret_raw = payload.get("base_resp", {}).get("ret", -1)
    try:
        ret = int(ret_raw)
    except Exception:
        ret = -1
    return ret == 0, merged_cookie, set_cookie_headers, payload


async def start_login(cookie_header: str) -> tuple[dict, list[str]]:
    session_id = new_session_id()
    response = await proxy_wechat_request(
        url=BIZ_LOGIN_ENDPOINT,
        cookie_header=cookie_header,
        method="POST",
        params={"action": "startlogin"},
        data={
            "userlang": "zh_CN",
            "redirect_url": "",
            "login_type": 3,
            "sessionid": session_id,
            "token": "",
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        },
    )
    payload = response.json() if "json" in response.headers.get("content-type", "") else {"base_resp": {"ret": 0}}
    return {"session_id": session_id, "payload": payload}, _filter_set_cookie_headers(response, is_https=False)


async def get_login_qrcode(cookie_header: str) -> tuple[bytes, str, list[str]]:
    response = await proxy_wechat_request(
        url=QR_ENDPOINT,
        cookie_header=cookie_header,
        params={"action": "getqrcode", "random": int(time.time() * 1000)},
    )
    media_type = response.headers.get("content-type", "image/png")
    return response.content, media_type, _filter_set_cookie_headers(response, is_https=False)


async def check_login_scan(cookie_header: str) -> tuple[dict, list[str]]:
    response = await proxy_wechat_request(
        url=QR_ENDPOINT,
        cookie_header=cookie_header,
        params={"action": "ask", "token": "", "lang": "zh_CN", "f": "json", "ajax": 1},
    )
    return response.json(), _filter_set_cookie_headers(response, is_https=False)


async def complete_login(cookie_header: str) -> tuple[dict, list[str]]:
    response = await proxy_wechat_request(
        url=BIZ_LOGIN_ENDPOINT,
        cookie_header=cookie_header,
        method="POST",
        params={"action": "login"},
        data={
            "userlang": "zh_CN",
            "redirect_url": "",
            "cookie_forbidden": 0,
            "cookie_cleaned": 0,
            "plugin_used": 0,
            "login_type": 3,
            "token": "",
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        },
    )
    payload = response.json()
    if payload.get("base_resp", {}).get("ret") != 0:
        error = normalize_utf8_text(payload.get("base_resp", {}).get("err_msg") or "微信公众号扫码登录失败").strip()
        log_wechat_auth_event(f"公众号扫码登录失败：{error}")
        raise RuntimeError(error)

    redirect_url = str(payload.get("redirect_url") or "").strip()
    parsed = urlparse(f"http://localhost{redirect_url}")
    token = parse_qs(parsed.query).get("token", [""])[0].strip()
    if not token:
        log_wechat_auth_event("公众号扫码登录失败：未能从登录结果中解析到 token")
        raise RuntimeError("未能从微信登录结果中解析到 token")

    merged_cookie = _merge_cookie_headers(cookie_header, response)
    login_set_cookie_headers = _filter_set_cookie_headers(response, is_https=False)
    api_ok, merged_cookie, api_set_cookie_headers, api_payload = await _probe_api_session(
        token=token,
        cookie_header=merged_cookie,
    )
    if not api_ok:
        log_wechat_auth_event(f"公众号扫码登录后接口校验失败：{api_payload}")
        raise RuntimeError("扫码成功，但微信公众号后台登录态尚未生效，请稍后重试")

    nickname, fakeid = await fetch_account_identity(token=token, cookie_header=merged_cookie)
    all_set_cookie_headers = _merge_set_cookie_values(login_set_cookie_headers, api_set_cookie_headers)
    expire_time = _resolve_expire_time_ms(all_set_cookie_headers)
    credentials = {
        "token": token,
        "cookie": merged_cookie,
        "fakeid": fakeid,
        "nickname": nickname,
        "expire_time": expire_time,
        "initial_expire_time": expire_time,
    }
    save_wechat_credentials(credentials)
    log_wechat_auth_event(
        f"公众号登录成功，账号={nickname or '公众号'}，"
        f"expire_time={_format_expire_time(expire_time)}，来源=login+api"
    )
    return credentials, all_set_cookie_headers


async def fetch_account_identity(*, token: str, cookie_header: str) -> tuple[str, str]:
    nickname = "公众号"
    fakeid = ""
    headers = {
        "Cookie": cookie_header,
        "User-Agent": WECHAT_USER_AGENT,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        info_response = await client.get(
            WECHAT_HOME_URL,
            params={"t": "home/index", "token": token, "lang": "zh_CN"},
            headers=headers,
        )
        html = info_response.text
        nick_match = re.search(r'nick_name\s*[:=]\s*["\']([^"\']+)["\']', html)
        if nick_match:
            nickname = normalize_utf8_text(nick_match.group(1)).strip() or nickname

        search_response = await client.get(
            WECHAT_SEARCH_BIZ_URL,
            params={
                "action": "search_biz",
                "token": token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": 1,
                "random": time.time(),
                "query": nickname,
                "begin": 0,
                "count": 5,
            },
            headers=headers,
        )
        search_payload = search_response.json()
        if search_payload.get("base_resp", {}).get("ret") == 0:
            accounts = search_payload.get("list", []) or []
            for account in accounts:
                if normalize_utf8_text(account.get("nickname")).strip() == nickname:
                    fakeid = str(account.get("fakeid") or "").strip()
                    break
            if not fakeid and accounts:
                fakeid = str(accounts[0].get("fakeid") or "").strip()
    return nickname, fakeid


async def _is_api_session_valid(*, token: str, cookie_header: str) -> bool:
    valid, _, _, _ = await _probe_api_session(token=token, cookie_header=cookie_header)
    return valid


async def renew_login_with_existing_credentials() -> tuple[dict[str, object], list[str]]:
    credentials = get_wechat_credentials()
    token = str(credentials.get("token") or "").strip()
    cookie = str(credentials.get("cookie") or "").strip()
    if not token or not cookie:
        raise RuntimeError("当前没有可续期的公众号登录态，请重新扫码登录")

    response = await proxy_wechat_request(
        url=WECHAT_HOME_URL,
        cookie_header=cookie,
        params={"t": "home/index", "token": token, "lang": "zh_CN"},
    )
    final_url = str(response.url)
    html = response.text
    merged_cookie = _merge_cookie_headers(cookie, response)
    home_set_cookie_headers = _filter_set_cookie_headers(response, is_https=False)
    api_ok, merged_cookie, api_set_cookie_headers, api_payload = await _probe_api_session(
        token=token,
        cookie_header=merged_cookie,
    )
    if (
        "/cgi-bin/home" not in final_url
        or "login" in final_url.lower()
        or "wx.passport" in html.lower()
        or "layout/error" in html.lower()
        or _contains_expired_session_cookies(home_set_cookie_headers)
        or not api_ok
    ):
        log_wechat_auth_event(f"免扫码续期失败：现有登录态已失效，需重新扫码。api={api_payload}")
        raise RuntimeError("当前公众号登录态已失效，无法免扫码续期，请重新扫码登录")

    all_set_cookie_headers = _merge_set_cookie_values(home_set_cookie_headers, api_set_cookie_headers)
    expire_time = _resolve_expire_time_ms(all_set_cookie_headers)
    nickname, fakeid = await fetch_account_identity(token=token, cookie_header=merged_cookie)
    renewed = {
        "token": token,
        "cookie": merged_cookie,
        "fakeid": fakeid or str(credentials.get("fakeid") or "").strip(),
        "nickname": nickname or normalize_utf8_text(credentials.get("nickname")).strip(),
        "expire_time": expire_time,
    }
    save_wechat_credentials(renewed)
    log_wechat_auth_event(
        f"公众号登录态免扫码续期成功，账号={renewed['nickname'] or '公众号'}，"
        f"expire_time={_format_expire_time(expire_time)}，来源=home+api"
    )
    return renewed, all_set_cookie_headers

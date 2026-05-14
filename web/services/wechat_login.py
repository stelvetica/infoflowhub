from __future__ import annotations

import re
import time
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
from connectors.auth.providers.wechat import save_wechat_credentials


MP_BASE_URL = "https://mp.weixin.qq.com"
QR_ENDPOINT = f"{MP_BASE_URL}/cgi-bin/scanloginqrcode"
BIZ_LOGIN_ENDPOINT = f"{MP_BASE_URL}/cgi-bin/bizlogin"
WECHAT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"


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


def _filter_set_cookie_headers(response: httpx.Response, is_https: bool) -> list[str]:
    values = response.headers.get_list("set-cookie")
    if is_https:
        return values
    return [value.replace("; Secure", "") for value in values]


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
        error = str(payload.get("base_resp", {}).get("err_msg") or "微信登录失败").strip()
        raise RuntimeError(error)

    redirect_url = str(payload.get("redirect_url") or "").strip()
    parsed = urlparse(f"http://localhost{redirect_url}")
    token = parse_qs(parsed.query).get("token", [""])[0].strip()
    if not token:
        raise RuntimeError("未从微信登录结果中解析到 token。")

    merged_cookie = _merge_cookie_headers(cookie_header, response)
    nickname, fakeid = await fetch_account_identity(token=token, cookie_header=merged_cookie)
    expire_time = int((time.time() + 4 * 24 * 3600) * 1000)
    credentials = {
        "token": token,
        "cookie": merged_cookie,
        "fakeid": fakeid,
        "nickname": nickname,
        "expire_time": expire_time,
    }
    save_wechat_credentials(credentials)
    return credentials, _filter_set_cookie_headers(response, is_https=False)


async def fetch_account_identity(*, token: str, cookie_header: str) -> tuple[str, str]:
    nickname = "公众号"
    fakeid = ""
    headers = {
        "Cookie": cookie_header,
        "User-Agent": WECHAT_USER_AGENT,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        info_response = await client.get(
            f"{MP_BASE_URL}/cgi-bin/home",
            params={"t": "home/index", "token": token, "lang": "zh_CN"},
            headers=headers,
        )
        html = info_response.text
        nick_match = re.search(r'nick_name\s*[:=]\s*["\']([^"\']+)["\']', html)
        if nick_match:
            nickname = nick_match.group(1).strip() or nickname

        search_response = await client.get(
            f"{MP_BASE_URL}/cgi-bin/searchbiz",
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
                if str(account.get("nickname") or "").strip() == nickname:
                    fakeid = str(account.get("fakeid") or "").strip()
                    break
            if not fakeid and accounts:
                fakeid = str(accounts[0].get("fakeid") or "").strip()
    return nickname, fakeid

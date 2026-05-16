from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from connectors._shared.common import is_transient_fetch_error
from connectors.wechat.auth import extract_wechat_fakeid, load_wechat_credentials


WECHAT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
WECHAT_ARTICLE_LIST_URL = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"
WECHAT_SEARCH_BIZ_URL = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
WECHAT_RETRY_DELAYS = (0.0, 1.5)


def _open_json_with_retry(request: urllib.request.Request, timeout: int = 30) -> dict:
    last_error: Exception | None = None
    for attempt, delay in enumerate(WECHAT_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="ignore"))
        except Exception as exc:
            last_error = exc
            if attempt >= len(WECHAT_RETRY_DELAYS) or not is_transient_fetch_error(str(exc)):
                raise
    raise last_error or RuntimeError("wechat request failed")


def fetch_wechat_articles(source: dict, begin: int = 0, count: int = 10, keyword: str = "") -> dict:
    credentials = load_wechat_credentials()
    token = str(credentials.get("token") or "").strip()
    cookie = str(credentials.get("cookie") or "").strip()
    fakeid = extract_wechat_fakeid(source)
    if not fakeid:
        article_url = extract_wechat_article_url(source)
        if article_url:
            fakeid = resolve_fakeid_from_article(article_url)
    if not fakeid:
        raise ValueError("微信公众号 source 缺少 fakeid，且未能从文章链接自动解析。")
    if not token or not cookie:
        raise ValueError("缺少微信公众号登录凭证。")

    params = {
        "sub": "search" if keyword else "list",
        "search_field": "7" if keyword else "null",
        "begin": begin,
        "count": count,
        "query": keyword,
        "fakeid": fakeid,
        "type": "101_1",
        "free_publish_type": 1,
        "sub_action": "list_ex",
        "token": token,
        "lang": "zh_CN",
        "f": "json",
        "ajax": 1,
    }
    url = f"{WECHAT_ARTICLE_LIST_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": WECHAT_USER_AGENT,
            "Referer": "https://mp.weixin.qq.com/",
            "Cookie": cookie,
            "Accept": "application/json, text/plain, */*",
        },
    )
    return _open_json_with_retry(request, timeout=30)


def search_wechat_accounts(query: str, count: int = 5) -> dict:
    credentials = load_wechat_credentials()
    token = str(credentials.get("token") or "").strip()
    cookie = str(credentials.get("cookie") or "").strip()
    if not query.strip():
        raise ValueError("公众号搜索关键词不能为空。")
    if not token or not cookie:
        raise ValueError("缺少微信公众号登录凭证。")
    params = {
        "action": "search_biz",
        "token": token,
        "lang": "zh_CN",
        "f": "json",
        "ajax": 1,
        "random": time.time(),
        "query": query.strip(),
        "begin": 0,
        "count": count,
    }
    url = f"{WECHAT_SEARCH_BIZ_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": WECHAT_USER_AGENT,
            "Referer": "https://mp.weixin.qq.com/",
            "Cookie": cookie,
            "Accept": "application/json, text/plain, */*",
        },
    )
    return _open_json_with_retry(request, timeout=30)


def parse_wechat_search_list(payload: dict) -> tuple[list[dict], str]:
    base_resp = payload.get("base_resp", {}) if isinstance(payload, dict) else {}
    ret = base_resp.get("ret", -1)
    if ret != 0:
        error_msg = str(base_resp.get("err_msg") or f"ret={ret}").strip()
        if "login" in error_msg.lower() or ret == 200003:
            return [], "微信公众号登录态已失效，请重新扫码登录。"
        return [], f"公众号搜索接口返回异常: {error_msg}"
    accounts = payload.get("list", []) or []
    results: list[dict] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        results.append(
            {
                "fakeid": str(account.get("fakeid") or "").strip(),
                "nickname": str(account.get("nickname") or "").strip(),
                "alias": str(account.get("alias") or "").strip(),
                "round_head_img": str(account.get("round_head_img") or "").strip(),
            }
        )
    return results, ""


def parse_wechat_publish_list(payload: dict) -> tuple[list[dict], str]:
    base_resp = payload.get("base_resp", {}) if isinstance(payload, dict) else {}
    ret = base_resp.get("ret", -1)
    if ret != 0:
        error_msg = str(base_resp.get("err_msg") or f"ret={ret}").strip()
        if "login" in error_msg.lower() or ret == 200003:
            return [], "微信公众号登录态已失效，请更新 WECHAT_TOKEN / WECHAT_COOKIE 后重试。"
        return [], f"微信公众号接口返回异常: {error_msg}"

    publish_page = payload.get("publish_page", {})
    if isinstance(publish_page, str):
        try:
            publish_page = json.loads(publish_page)
        except Exception:
            return [], "微信公众号返回的 publish_page 解析失败。"
    if not isinstance(publish_page, dict):
        return [], "微信公众号返回的 publish_page 结构异常。"

    publish_list = publish_page.get("publish_list", [])
    articles: list[dict] = []
    for item in publish_list:
        publish_info = item.get("publish_info", {})
        if isinstance(publish_info, str):
            try:
                publish_info = json.loads(publish_info)
            except Exception:
                continue
        if not isinstance(publish_info, dict):
            continue
        for article in publish_info.get("appmsgex", []) or []:
            if not isinstance(article, dict):
                continue
            articles.append(
                {
                    "aid": str(article.get("aid") or "").strip(),
                    "title": str(article.get("title") or "").strip(),
                    "link": str(article.get("link") or "").strip(),
                    "digest": str(article.get("digest") or "").strip(),
                    "author": str(article.get("author") or "").strip(),
                    "update_time": int(article.get("update_time") or 0),
                    "create_time": int(article.get("create_time") or 0),
                }
            )
    return articles, ""


def extract_wechat_article_url(source: dict) -> str:
    feed_url = str(source.get("feed_url") or "").strip()
    site_url = str(source.get("site_url") or "").strip()
    for value in (feed_url, site_url):
        if value.startswith("wechat://article?url="):
            encoded = value.split("wechat://article?url=", 1)[1]
            return urllib.parse.unquote(encoded)
        if value.startswith("https://mp.weixin.qq.com/s/"):
            return value
    return ""


def parse_wechat_article_meta(article_url: str) -> dict[str, str]:
    request = urllib.request.Request(article_url, headers={"User-Agent": WECHAT_USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="ignore")
    nickname_match = re.search(r'var\s+nickname\s*=\s*htmlDecode\("([^"]+)"\)', html)
    user_name_match = re.search(r'var\s+user_name\s*=\s*"([^"]+)"', html)
    title_match = re.search(r"<title>([^<]+)</title>", html, flags=re.IGNORECASE)
    return {
        "nickname": str(nickname_match.group(1) if nickname_match else "").strip(),
        "user_name": str(user_name_match.group(1) if user_name_match else "").strip(),
        "title": str(title_match.group(1) if title_match else "").strip(),
    }


def resolve_fakeid_from_article(article_url: str) -> str:
    article_meta = parse_wechat_article_meta(article_url)
    nickname = article_meta.get("nickname", "")
    if not nickname:
        return ""
    payload = search_wechat_accounts(nickname, count=10)
    items, err = parse_wechat_search_list(payload)
    if err:
        return ""
    for item in items:
        if item.get("nickname") == nickname:
            return str(item.get("fakeid") or "").strip()
    return str(items[0].get("fakeid") or "").strip() if items else ""

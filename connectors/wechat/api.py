from __future__ import annotations

import json
import urllib.parse
import urllib.request

from connectors.wechat.auth import extract_wechat_fakeid, load_wechat_credentials


WECHAT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
WECHAT_ARTICLE_LIST_URL = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"


def fetch_wechat_articles(source: dict, begin: int = 0, count: int = 10, keyword: str = "") -> dict:
    credentials = load_wechat_credentials()
    token = str(credentials.get("token") or "").strip()
    cookie = str(credentials.get("cookie") or "").strip()
    fakeid = extract_wechat_fakeid(source)
    if not fakeid:
        raise ValueError("微信公众号 source 缺少 fakeid，请使用 wechat://mp/<fakeid> 形式的 feed_url。")
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
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    return payload


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

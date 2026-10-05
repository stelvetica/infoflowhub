"""路由层游标接线：URL 里的 *_after 必须到达视图，且不得写进状态 cookie。

回退样例：游标式续载上线后 builder 白名单丢掉 news_after，续载永远返回首批
（前端照常追加，滚动全部是重复行）。
"""

import unittest
from urllib.parse import parse_qsl

from fastapi import Response
from starlette.requests import Request

from web.routes.main import _load_state, _persist_state
from web.services.views import build_news_state_params


def make_request(query: str, cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": query.encode(), "headers": headers})


class RouteCursorPassthroughTest(unittest.TestCase):
    def test_query_cursor_reaches_view_params(self):
        params = _load_state(make_request("news_scope=all&news_after=abc%7Cdef"), "ifh_news_state_v1", build_news_state_params)

        self.assertEqual(params["news_after"], "abc|def")
        self.assertEqual(params["news_scope"], "all")

    def test_cookie_cannot_override_query_cursor(self):
        cookie = "ifh_news_state_v1=" + "news_after=stale%7Ccursor&news_scope=flow"
        params = _load_state(make_request("news_after=fresh%7Ccursor", cookie), "ifh_news_state_v1", build_news_state_params)

        self.assertEqual(params["news_after"], "fresh|cursor")
        self.assertEqual(params["news_scope"], "flow")

    def test_cursor_is_not_persisted_into_cookie(self):
        response = Response()
        _persist_state(response, "ifh_news_state_v1", {"news_scope": "all", "news_after": "abc|def"})

        raw = response.headers["set-cookie"].split(";")[0].split("=", 1)[1].strip('"')
        cookie_dict = dict(parse_qsl(raw, keep_blank_values=True))
        self.assertEqual(cookie_dict.get("news_scope"), "all")
        self.assertNotIn("news_after", cookie_dict)


if __name__ == "__main__":
    unittest.main()

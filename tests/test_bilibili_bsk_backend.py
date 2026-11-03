"""B 站 12 个源迁到 bsk 后端：页内 fetch 取数、字段投影、分派与共用会话。

假 page / 假会话打桩，不连真实 daemon 与 Chrome。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.subscriptions import rss_config, rss_db, runtime_health
from apps.subscriptions.models import FeedEntry, FeedFetchResult
from connectors._shared import web_fetch
from connectors._shared.bsk_session import BskRunnerSession
from connectors._shared.common import normalize_dedupe_title_key
from connectors.bilibili import api as bili_api
from connectors.bilibili import fetch_bilibili_dynamic_feed_with_page

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ID = "Mark__Huang"
SOURCE = {
    "id": SRC_ID,
    "name": "观点 Mark__Huang",
    "feed_url": "https://rsshub.app/bilibili/user/dynamic/424405794",
    "site_url": "https://space.bilibili.com/424405794/dynamic",
    "auth_key": "",
    "provider": "web",
    "fetch_via": "web",
}

# 一条「HTTP 原样」动态：包含解析器会读的字段 + 无关噪声（投影应丢弃噪声但保留被读字段）。
FULL_ITEM = {
    "id_str": "1249317570239004681",
    "id": 1249317570239004681,
    "type": "DYNAMIC_TYPE_AV",
    "basic": {"jump_url": "//www.bilibili.com/video/BV18ze86oEXv", "comment_id_str": "1", "like_icon": {"x": 1}},
    "modules": {
        "module_author": {
            "name": "Mark__Huang",
            "pub_ts": 1789830900,
            "pub_time": "2026年09月19日 23:15",
            "pub_action": "投稿了视频",
            "face": "https://i0.hdslb.com/noise.jpg",
        },
        "module_dynamic": {
            "desc": {"text": None, "rich_text_nodes": [{"orig_text": "噪声"}]},
            "major": {
                "archive": {
                    "title": "【直播回放】周末聊聊天 2026年09月19日22点场",
                    "desc": "desc-noise",
                    "jump_url": "//www.bilibili.com/video/BV18ze86oEXv",
                    "cover": "https://noise/cover.jpg",
                    "stat": {"play": 123},
                }
            },
        },
        "module_stat": {"like": {"count": 5}},
    },
}

# bsk 页内投影后的同一条（字段顺序/类型与 _BILIBILI_DYNAMIC_PAGE_JS 一致；bsk 会把数字序列化成字符串）。
PROJECTED_ITEM = {
    "id_str": "1249317570239004681",
    "id": "1249317570239004681",
    "type": "DYNAMIC_TYPE_AV",
    "basic": {"jump_url": "//www.bilibili.com/video/BV18ze86oEXv"},
    "modules": {
        "module_author": {
            "name": "Mark__Huang",
            "pub_ts": "1789830900",
            "pub_time": "2026年09月19日 23:15",
            "pub_action": "投稿了视频",
        },
        "module_dynamic": {
            "desc": {},
            "additional": {"desc": {}},
            "major": {
                "opus": {"summary": {}},
                "archive": {
                    "title": "【直播回放】周末聊聊天 2026年09月19日22点场",
                    "desc": "desc-noise",
                    "jump_url": "//www.bilibili.com/video/BV18ze86oEXv",
                },
                "article": {},
                "common": {},
                "music": {},
                "pgc": {},
                "medialist": {},
                "ugc_season": {},
                "live": {},
                "live_rcmd": {},
            },
        },
    },
}


class FakeBskPage:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.goto_url = ""
        self.evaluated: list = []

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.goto_url = url

    def evaluate(self, js, arg=None, **kwargs):
        self.evaluated.append((js, arg, kwargs))
        return self.payload


# ---- 页内取数与字段投影 ----


def test_page_path_parses_projected_items():
    page = FakeBskPage({"code": 0, "message": "0", "items": [dict(PROJECTED_ITEM)]})

    result = fetch_bilibili_dynamic_feed_with_page(page, SOURCE)

    assert result.ok and len(result.entries) == 1
    entry = result.entries[0]
    assert entry.title == "【直播回放】周末聊聊天 2026年09月19日22点场"
    assert entry.link == "https://www.bilibili.com/video/BV18ze86oEXv"
    assert entry.summary == "投稿了视频"
    assert entry.published == "2026/09/19 23:15"
    # 先导航到该源自己的 space 页（同域上下文 + Referer）
    assert page.goto_url == "https://space.bilibili.com/424405794/dynamic"


def test_page_projection_preserves_every_field_the_parser_reads():
    """投影必须保留解析器读到的全部字段：同一动态，HTTP 原样与页内投影解析结果一致。"""
    full = bili_api._parse_dynamic_item(json.loads(json.dumps(FULL_ITEM)))
    projected = bili_api._parse_dynamic_item(json.loads(json.dumps(PROJECTED_ITEM)))

    for key in ("id", "type", "title", "summary", "link", "dynamic_link", "published_ts", "published_at", "author"):
        assert full[key] == projected[key], key


def test_projection_js_only_reads_whitelisted_keys():
    """投影 JS 不做整条透传：不含 raw/整对象回传，且覆盖解析器要用的字段。"""
    js = bili_api._BILIBILI_DYNAMIC_PAGE_JS
    assert "jump_url" in js and "pub_ts" in js and "id_str" in js
    assert "rich_text_nodes" not in js and "module_stat" not in js
    # 回传体里没有 raw 字段（HTTP 路径才会把整条塞进 raw）
    assert "raw" not in js


def test_page_path_passes_uid_and_enlarged_timeout():
    page = FakeBskPage({"code": 0, "message": "0", "items": [dict(PROJECTED_ITEM)]})

    fetch_bilibili_dynamic_feed_with_page(page, SOURCE, timeout_ms=60000)

    js, arg, kwargs = page.evaluated[0]
    assert json.loads(arg)["uid"] == "424405794"
    # evaluate 预算必须明显大于 30s 看门狗，避免被误判成 BskHelpTimeout
    assert kwargs["timeout"] > 30


def test_page_path_dedups_and_applies_limit():
    page = FakeBskPage(
        {"code": 0, "message": "0", "items": [dict(PROJECTED_ITEM), dict(PROJECTED_ITEM), dict(PROJECTED_ITEM)]}
    )

    result = fetch_bilibili_dynamic_feed_with_page(page, SOURCE)

    assert result.ok and len(result.entries) == 1  # 同一 id_str 只留一条


def test_page_path_api_error_becomes_failure_with_code():
    page = FakeBskPage({"code": -352, "message": "风控校验失败", "items": []})

    result = fetch_bilibili_dynamic_feed_with_page(page, SOURCE)

    assert not result.ok
    assert "code=-352" in result.error and "风控校验失败" in result.error


def test_page_path_request_error_becomes_failure():
    page = FakeBskPage({"error": "接口请求失败: TypeError: Failed to fetch"})

    result = fetch_bilibili_dynamic_feed_with_page(page, SOURCE)

    assert not result.ok and "Failed to fetch" in result.error


def test_page_path_goto_exception_becomes_failure():
    class BoomPage(FakeBskPage):
        def goto(self, url, wait_until=None, timeout=None):
            raise RuntimeError("navigate 失败")

    result = fetch_bilibili_dynamic_feed_with_page(BoomPage({}), SOURCE)

    assert not result.ok and "navigate 失败" in result.error


# ---- 分派：bsk → page；非 bsk → HTTP；任何配置都不构造 copy ----


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "rss_settings.json"

    def write(section: dict | None) -> None:
        path.write_text(json.dumps({"browser_backend": section} if section is not None else {}), encoding="utf-8")

    monkeypatch.setattr(rss_config, "SETTINGS_PATH", path)
    return write


class FakeBskSession:
    """记录 start/shutdown/acquire_page 的假 bsk 会话。"""

    def __init__(self) -> None:
        self.start_calls = 0
        self.shutdown_calls = 0
        self.page = object()

    def start(self) -> None:
        self.start_calls += 1

    def shutdown(self, **kwargs) -> None:
        self.shutdown_calls += 1

    def acquire_page(self):
        return self.page


def _ok_result(source: dict) -> FeedFetchResult:
    return FeedFetchResult(
        source_id=source["id"],
        source_name=source["name"],
        feed_url=source["feed_url"],
        ok=True,
        status=200,
        entries=[FeedEntry(source["id"], source["name"], "标题", "https://t.bilibili.com/1", "2026/09/19 23:15", "")],
    )


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("connectors._shared.bsk_session.BSK_RETRY_DELAY_SECONDS", 0.0)


def test_bilibili_bsk_uses_page_path_and_never_copy(settings_file, monkeypatch):
    settings_file({})
    session = FakeBskSession()
    seen_pages: list = []
    monkeypatch.setattr(web_fetch, "_make_session_for", lambda source: session)
    monkeypatch.setattr(
        web_fetch,
        "fetch_bilibili_dynamic_feed_with_page",
        lambda page, source, limit=12, timeout_ms=60000: seen_pages.append(page) or _ok_result(source),
    )

    result = web_fetch._fetch_bilibili_one(SOURCE)

    assert result.ok
    assert seen_pages == [session.page]  # 页内 fetch 真的落到 bsk page 上
    assert session.start_calls == 1 and session.shutdown_calls == 1


def test_bilibili_never_in_shared_runner_sites():
    """把 B 站留在 _SHARED_RUNNER_SITES 之外：批量抓取时它单独分组复用同一个 bsk 会话。"""
    assert "bilibili" not in web_fetch._SHARED_RUNNER_SITES
    assert web_fetch._needs_shared_runner(SOURCE) is False


# ---- 批量：12 源共用同一个 bsk 会话 ----


def test_batch_bilibili_reuses_single_bsk_session(settings_file, monkeypatch):
    ids = [f"bili{i}" for i in range(12)]
    settings_file({"sources": {sid: "bsk" for sid in ids}})
    sources = [
        {
            "id": sid,
            "name": sid,
            "feed_url": f"https://rsshub.app/bilibili/user/dynamic/{i}",
            "site_url": f"https://space.bilibili.com/{i}/dynamic",
            "provider": "web",
            "fetch_via": "web",
        }
        for i, sid in enumerate(ids)
    ]
    session = FakeBskSession()
    monkeypatch.setattr(web_fetch, "_make_session_for", lambda source: session)
    fetched: list = []
    monkeypatch.setattr(
        web_fetch,
        "_fetch_bilibili_via_bsk_session",
        lambda sess, source, limit=12, timeout_ms=60000: fetched.append((sess, source["id"])) or _ok_result(source),
    )

    results = web_fetch.fetch_web_many(sources)

    assert len(results) == 12 and all(r.ok for r in results)
    assert session.start_calls == 1  # 12 源只 start 一次
    assert session.shutdown_calls == 1
    assert [sid for _, sid in fetched] == ids  # 每源各抓一次，顺序稳定


# ---- 配置一致性 ----


def test_twelve_bilibili_sources_are_bsk_only():
    settings = json.loads((REPO_ROOT / "config" / "rss_settings.json").read_text(encoding="utf-8"))
    sources = json.loads((REPO_ROOT / "config" / "subscription_sources.json").read_text(encoding="utf-8"))["sources"]
    bili_sources = [s for s in sources if "bilibili.com" in str(s.get("feed_url") or "") + str(s.get("site_url") or "")]

    assert len(bili_sources) == 12
    # B站没有独立认证资产：auth_key 必须为空，登录态全部来自 bsk 复用的日常 Chrome
    assert all(not s.get("auth_key") for s in bili_sources)
    # copy 后端已删除：配置里不再有可切换后端的字段
    assert "sources" not in settings["browser_backend"]

# ---- 增量翻页：按已入库标题连续命中 K 条停止 ----


class SequencedFakePage(FakeBskPage):
    """按序返回多页 payload 的假 page；超出后重复最后一页。"""

    def __init__(self, payloads: list) -> None:
        super().__init__(payloads[0])
        self.payloads = list(payloads)
        self.evaluated = []

    def evaluate(self, js, arg=None, **kwargs):
        index = len(self.evaluated)
        self.evaluated.append((js, arg, kwargs))
        return self.payloads[min(index, len(self.payloads) - 1)]


def _projected(n: int, title: str | None = None) -> dict:
    item = json.loads(json.dumps(PROJECTED_ITEM))
    item["id_str"] = item["id"] = f"id{n}"
    item["modules"]["module_dynamic"]["major"]["archive"]["title"] = title or f"动态标题 {n}"
    return item


def _payload(items: list, *, offset: str = "", has_more: bool = False, code: int = 0, message: str = "0") -> dict:
    return {"code": code, "message": message, "items": items, "offset": offset, "has_more": has_more}


def _with_known(keys: list[str]) -> dict:
    return {**SOURCE, "known_entry_keys": keys}


def test_fresh_library_scans_to_max_pages():
    pages = []
    n = 0
    for page_index in range(6):
        items = []
        for _ in range(20):
            n += 1
            items.append(_projected(n))
        pages.append(_payload(items, offset=f"offset-{page_index + 1}", has_more=True))
    page = SequencedFakePage(pages)

    result = fetch_bilibili_dynamic_feed_with_page(page, SOURCE, limit=200)

    assert len(page.evaluated) == 6  # 达到安全上限
    assert result.ok and len(result.entries) == 120
    assert result.stop_reason == bili_api.BILIBILI_STOP_MAX_PAGES


def test_duplicate_streak_stops_on_first_page():
    items = [_projected(1, "旧 A"), _projected(2, "旧 B"), _projected(3, "旧 C")]
    page = SequencedFakePage([_payload(items, offset="next", has_more=True)])

    result = fetch_bilibili_dynamic_feed_with_page(page, _with_known(["旧 a", "旧 b", "旧 c"]))

    assert len(page.evaluated) == 1
    assert result.stop_reason == bili_api.BILIBILI_STOP_DUPLICATE_STREAK
    assert len(result.entries) == 3


def test_duplicate_streak_reached_across_pages():
    first = [_projected(1, "新 1"), _projected(2, "新 2"), _projected(3, "旧"), _projected(4, "旧")]
    second = [_projected(5, "旧"), _projected(6, "旧"), _projected(7, "旧")]
    page = SequencedFakePage(
        [
            _payload(first, offset="next", has_more=True),
            _payload(second, offset="next2", has_more=True),
        ]
    )

    result = fetch_bilibili_dynamic_feed_with_page(page, _with_known(["旧"]))

    assert len(page.evaluated) == 2
    assert result.stop_reason == bili_api.BILIBILI_STOP_DUPLICATE_STREAK
    assert len(result.entries) == 7


def test_pinned_duplicate_id_does_not_touch_streak():
    pinned = _projected(1, "新")
    items = [pinned, dict(pinned), _projected(2, "旧 1"), _projected(3, "旧 2"), _projected(4, "旧 3")]
    page = SequencedFakePage([_payload(items, offset="next", has_more=True)])

    result = fetch_bilibili_dynamic_feed_with_page(page, _with_known(["旧 1", "旧 2", "旧 3"]))

    assert len(page.evaluated) == 1
    assert result.stop_reason == bili_api.BILIBILI_STOP_DUPLICATE_STREAK
    assert len(result.entries) == 4  # 重复 id 只入一条


def test_end_of_feed_stops_without_more_pages():
    page = SequencedFakePage([_payload([_projected(1), _projected(2)], has_more=False)])

    result = fetch_bilibili_dynamic_feed_with_page(page, SOURCE)

    assert len(page.evaluated) == 1
    assert result.stop_reason == bili_api.BILIBILI_STOP_END_OF_FEED
    assert len(result.entries) == 2


def test_limit_truncates_entries_and_stops():
    page = SequencedFakePage([_payload([_projected(1), _projected(2), _projected(3)], offset="next", has_more=True)])

    result = fetch_bilibili_dynamic_feed_with_page(page, SOURCE, limit=2)

    assert len(page.evaluated) == 1
    assert result.stop_reason == bili_api.BILIBILI_STOP_LIMIT
    assert len(result.entries) == 2


def test_second_page_api_error_fails_whole_source():
    page = SequencedFakePage(
        [
            _payload([_projected(1)], offset="next", has_more=True),
            _payload([], code=-352, message="风控校验失败"),
        ]
    )

    result = fetch_bilibili_dynamic_feed_with_page(page, SOURCE)

    assert not result.ok
    assert "code=-352" in result.error
    assert result.stop_reason == ""


def test_dedupe_title_key_has_single_definition():
    sample = "  A   b "
    assert rss_db.normalize_title_key(sample) == normalize_dedupe_title_key(sample) == "a b"


# ---- 应用层：抓取前注入已入库标题键 ----


def _install_fetch_stubs(tmp_path, monkeypatch, list_entries) -> list[dict]:
    monkeypatch.setattr(runtime_health, "HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(runtime_health, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(runtime_health, "save_entries", lambda entries: 0)
    monkeypatch.setattr(runtime_health, "list_entries", list_entries)
    captured: list[dict] = []

    def fake_fetch_many(sources, settings=None, timeout=45, **kwargs):
        captured.extend(sources)
        return [_ok_result(source) for source in sources]

    monkeypatch.setattr(runtime_health, "fetch_many", fake_fetch_many)
    return captured


RSS_SOURCE = {
    "id": "rss-demo",
    "name": "RSS 演示",
    "feed_url": "https://example.com/feed.xml",
    "site_url": "https://example.com/news",
}


def test_run_source_fetch_injects_known_keys_only_for_bilibili(tmp_path, monkeypatch):
    captured = _install_fetch_stubs(
        tmp_path, monkeypatch, lambda limit=200, source_id="": [{"title": "  旧   标题 "}]
    )

    runtime_health.run_source_fetch([SOURCE, RSS_SOURCE], persist_global=False)

    bili = next(source for source in captured if source["id"] == SRC_ID)
    assert bili["known_entry_keys"] == ["旧 标题"]
    rss = next(source for source in captured if source["id"] == "rss-demo")
    assert "known_entry_keys" not in rss
    # 注入走浅拷贝，调用方入参不被污染
    assert "known_entry_keys" not in SOURCE


def test_run_source_fetch_degrades_to_empty_keys_on_db_error(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    captured = _install_fetch_stubs(tmp_path, monkeypatch, boom)

    outcome = runtime_health.run_source_fetch([SOURCE], persist_global=False)

    assert not outcome.fatal_error
    bili = next(source for source in captured if source["id"] == SRC_ID)
    assert bili["known_entry_keys"] == []


def test_known_entry_keys_skips_lookup_when_source_id_missing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        runtime_health,
        "list_entries",
        lambda *args, **kwargs: calls.append((args, kwargs)) or [{"title": "跨源标题"}],
    )
    source = {key: value for key, value in SOURCE.items() if key != "id"}

    assert runtime_health._bilibili_known_entry_keys(source) == []
    assert calls == []  # 空 id 不得触发 list_entries（空串语义是全部源）

"""韭研公社正文落盘（离线夹具，不联网）+ 蓝宝书 markdown 输出逐字节回归。"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from apps.subscriptions import rss_db
from connectors._shared import md_store
from connectors.alphapai import feed as alphapai_feed
from connectors.jiuyangongshe import feed as jygs


FIXTURES = Path(__file__).resolve().parent / "fixtures"
QUOTES_HTML = "jiuyangongshe_article_quotes.html"
DISCLAIMER_HTML = "jiuyangongshe_article_disclaimer.html"
LIST_HTML = "jiuyangongshe_list.html"
LIST_IDS = ("71y7a6sklp", "2gt9adzuhvm", "11d6y337d8n", "ab5vtvuffm")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _source() -> dict:
    return {
        "id": "jiuyangongshe_jiucaituanzi",
        "name": "资讯 韭菜团子",
        "feed_url": "https://www.jiuyangongshe.com/u/1",
    }


# ---------- 正文提取 ----------


def test_extract_body_keeps_text_after_quotes():
    """正文含 \\" 与 &quot;，引号之后的文字必须仍在（证明 JS 字符串扫描未被截断）。"""
    body = jygs._extract_article_body(_fixture(QUOTES_HTML))

    assert len(body) > 100
    assert '"需求非常强劲"' in body          # &quot; 还原为真实引号
    assert '"AI capex"' in body              # 正文里的 \\" 未截断扫描
    assert "现金流拐点" in body              # 引号之后的尾段关键字仍在
    assert "follow-through" in body          # 中英混排
    assert "推荐位正文不应被抓取" not in body  # 不误取推荐位对象的 content
    # 相邻重复行只保留一行
    assert body.count("相邻重复行测试：同一行连续出现两次。") == 1


def test_extract_body_drops_disclaimer_lines():
    """payload 内出现免责段时，丢弃该行及其后全部内容。"""
    body = jygs._extract_article_body(_fixture(DISCLAIMER_HTML))

    assert len(body) > 100
    assert "作者利益披露" not in body
    assert "声明：文章观点来自网友" not in body
    assert "免责声明" not in body
    assert "正文末段" in body                # 免责段之前的内容保留
    assert "& 与" in body                    # &amp; 还原


def test_extract_body_ignores_variable_reference_content():
    """文章对象 content 为空（变量引用 content:b）时，不得退化成取推荐位的正文。"""
    html = (
        '<script>window.__NUXT__={data:[{data:{article_id:f,user_id:g,title:"短动态",'
        'content:b,url:b,type:g}},commendPage:{result:[{article_id:"rec0001",'
        'content:"\\u003Cp\\u003E推荐位正文\\u003C\\u002Fp\\u003E"}]}}]}</script>'
    )

    body = jygs._extract_article_body(html)

    assert body == ""


def test_extract_body_without_payload_is_empty():
    assert jygs._extract_article_body("<html><body><div class='text-box'></div></body></html>") == ""


def test_extract_body_keeps_article_links_as_markdown():
    """公社内容精选是「带链接的文章清单」，<a> 必须转成 markdown 链接而不是随标签丢掉。"""
    html = (
        '<script>window.__NUXT__={data:[{data:{article_id:f,title:"精选",content:"'
        '\\u003Cp\\u003E\\u003Ca target=\\"_blank\\" href=\\"https://www.jiuyangongshe.com/a/2z9a2wbfwtt\\" '
        'style=\\"color:#337DF2\\"\\u003E玻璃基板：产业渐行渐近\\u003C\\u002Fa\\u003E\\u003C\\u002Fp\\u003E"'
        '}}]}</script>'
    )

    body = jygs._extract_article_body(html)

    assert "[玻璃基板：产业渐行渐近](https://www.jiuyangongshe.com/a/2z9a2wbfwtt)" in body


def test_extract_body_downgrades_non_http_link_to_plain_text():
    """非 http(s) 链接（如 javascript:）只留标题文本，不产出可点击的 md 链接。"""
    html = (
        '<script>window.__NUXT__={data:[{data:{article_id:f,title:"t",content:"'
        '\\u003Cp\\u003E\\u003Ca href=\\"javascript:alert(1)\\"\\u003E点我\\u003C\\u002Fa\\u003E\\u003C\\u002Fp\\u003E"'
        '}}]}</script>'
    )

    body = jygs._extract_article_body(html)

    assert body == "点我"


def test_extract_meta_prefers_ssr_nodes():
    title, published = jygs._extract_article_meta(_fixture(QUOTES_HTML), "2gt9adzuhvm", "回退时间")

    assert title == '测试文章：AI算力链"再定价"与 English Mix'
    assert published == "2026-09-20 18:10:21"  # 前 19 字符，丢掉尾部地域 "上海"


def test_extract_meta_falls_back_to_payload_title_then_id():
    payload_only = (
        '<script>window.__NUXT__={data:[{data:{article_id:f,title:"payload 标题",'
        'content:"\\u003Cp\\u003E正文\\u003C\\u002Fp\\u003E"}}]}</script>'
    )
    assert jygs._extract_article_meta(payload_only, "2gt9adzuhvm", "2026-09-20 18:10:21") == (
        "payload 标题",
        "2026-09-20 18:10:21",
    )

    empty = "<html><body>无标题无时间</body></html>"
    assert jygs._extract_article_meta(empty, "2gt9adzuhvm", "2026-09-20 18:10:21") == (
        "韭研公社_2gt9adzu",
        "2026-09-20 18:10:21",
    )


# ---------- 落盘契约 ----------


def test_write_article_markdown_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(jygs, "MARKDOWN_DIR", tmp_path)

    path = Path(
        jygs._write_article_markdown(
            _source(),
            "2gt9adzuhvm",
            "测试文章：AI算力链再定价",
            "2026-09-20 18:10:21",
            "正文第一段\n\n正文第二段",
        )
    )

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_.+_[0-9a-f]{12}\.md", path.name)
    assert path.name.startswith("2026-09-20_韭菜团子_")
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == (
        "# 测试文章：AI算力链再定价\n"
        "\n"
        "- 发布时间: 2026/09/20 18:10\n"
        "- 来源: 韭研公社 韭菜团子\n"
        "- 原始页: https://www.jiuyangongshe.com/a/2gt9adzuhvm\n"
        "\n"
        "正文第一段\n\n正文第二段\n"
    )


def test_get_existing_article_ids_reads_links(tmp_path, monkeypatch):
    db_path = tmp_path / "subscriptions.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE rss_entries (source_id TEXT, link TEXT)")
    conn.execute(
        "INSERT INTO rss_entries VALUES ('jiuyangongshe_jiucaituanzi', 'https://www.jiuyangongshe.com/a/2gt9adzuhvm')"
    )
    conn.execute("INSERT INTO rss_entries VALUES ('jiuyangongshe_jiucaituanzi', '')")
    conn.execute("INSERT INTO rss_entries VALUES ('other_source', 'https://www.jiuyangongshe.com/a/ab5vtvuffm')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(rss_db, "DB_PATH", db_path)

    assert jygs._get_existing_article_ids("jiuyangongshe_jiucaituanzi") == {"2gt9adzuhvm"}
    monkeypatch.setattr(rss_db, "DB_PATH", tmp_path / "missing.sqlite3")
    assert jygs._get_existing_article_ids("jiuyangongshe_jiucaituanzi") == set()


# ---------- 整源离线跑通 ----------


def _install_offline_fetch(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_fetch_html(url: str) -> str:
        calls.append(url)
        if "/u/" in url:
            return _fixture(LIST_HTML)
        if url.endswith("/a/2gt9adzuhvm"):
            return _fixture(QUOTES_HTML)
        return _fixture(DISCLAIMER_HTML)

    monkeypatch.setattr(jygs, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(jygs, "MARKDOWN_DIR", tmp_path)
    monkeypatch.setattr(jygs.time, "sleep", lambda seconds: None)
    return calls


def test_fetch_writes_markdown_only_for_new_entries(tmp_path, monkeypatch):
    calls = _install_offline_fetch(monkeypatch, tmp_path)
    monkeypatch.setattr(jygs, "_get_existing_article_ids", lambda source_id: set())

    result = jygs.fetch_jiuyangongshe_with_page(None, _source(), limit=5)

    assert result.ok and len(result.entries) == len(LIST_IDS)
    assert len([url for url in calls if "/a/" in url]) == len(LIST_IDS)
    for entry in result.entries:
        assert entry.markdown_path
        path = Path(entry.markdown_path)
        assert path.is_file() and path.parent == tmp_path
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# ")
        assert "- 来源: 韭研公社 韭菜团子" in text
        assert f"- 原始页: {entry.link}" in text
        assert "作者利益披露" not in text
        assert "声明：文章观点来自网友" not in text

    # 已入库条目：不重复抓正文、markdown_path 留空
    calls.clear()
    monkeypatch.setattr(jygs, "_get_existing_article_ids", lambda source_id: set(LIST_IDS))
    second = jygs.fetch_jiuyangongshe_with_page(None, _source(), limit=5)

    assert len(second.entries) == len(LIST_IDS)
    assert all(entry.markdown_path == "" for entry in second.entries)
    assert not [url for url in calls if "/a/" in url]


def test_fetch_with_body_false_skips_article_pages(tmp_path, monkeypatch):
    calls = _install_offline_fetch(monkeypatch, tmp_path)

    result = jygs.fetch_jiuyangongshe_with_page(None, _source(), limit=5, with_body=False)

    assert result.ok and len(result.entries) == len(LIST_IDS)
    assert all(entry.markdown_path == "" for entry in result.entries)
    assert not [url for url in calls if "/a/" in url]


def test_fetch_falls_back_to_summary_when_article_fetch_fails(tmp_path, monkeypatch):
    def fake_fetch_html(url: str) -> str:
        if "/u/" in url:
            return _fixture(LIST_HTML)
        raise OSError("模拟正文页抓取失败")

    monkeypatch.setattr(jygs, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(jygs, "MARKDOWN_DIR", tmp_path)
    monkeypatch.setattr(jygs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(jygs, "_get_existing_article_ids", lambda source_id: set())

    result = jygs.fetch_jiuyangongshe_with_page(None, _source(), limit=5)

    assert result.ok and len(result.entries) == len(LIST_IDS)
    for entry in result.entries:
        assert Path(entry.markdown_path).is_file()  # 回退列表摘要仍落盘
        assert entry.summary in Path(entry.markdown_path).read_text(encoding="utf-8")


# ---------- 蓝宝书输出逐字节回归 ----------


def test_alphapai_markdown_output_byte_identical(tmp_path, monkeypatch):
    """内联快照：改造前实测的文件名与全文（改造后必须逐字节一致）。"""
    monkeypatch.setattr(alphapai_feed, "ALPHAPAI_MARKDOWN_DIR", tmp_path)
    title = "国内蓝宝书 9月21日晨会版 | 沪指低开高走，成交额1.2万亿，AI算力链集体走强"
    published = "2026/09/21 08:12"
    detail_text = (
        "国内蓝宝书 9月21日晨会版\n今天 08:12\n分享\n播放 12:34\n时长 08:00\n"
        "根据Alpha派机构投研用户实时研究动态聚合整理生成\n"
        "沪指低开高走，成交额1.2万亿。\n沪指低开高走，成交额1.2万亿。\n"
        "AI算力链集体走强，关注：\n中际旭创、新易盛\n免责声明：本文不构成投资建议\n"
    ).strip()
    content_title = "国内蓝宝书 9月21日晨会版"

    path = Path(
        md_store.write_markdown_file(
            tmp_path,
            title=title,
            published=published,
            source_line=alphapai_feed.SOURCE_LINE,
            page_url=alphapai_feed.BLUEBOOK_URL,
            body_text=alphapai_feed._normalize_detail_text(
                detail_text, content_title, "今天 08:12"
            ),
            display_title=content_title,
        )
    )

    assert path.name == "2026-09-21_国内蓝宝书 9月21日晨会版 - 沪指低开高走，成交额1.2万亿，AI算力链集体走强_c3fa3407d457.md"
    assert path.read_text(encoding="utf-8") == (
        "# 国内蓝宝书 9月21日晨会版\n"
        "\n"
        "- 发布时间: 2026/09/21 08:12\n"
        "- 来源: Alpha派 蓝宝书\n"
        "- 原始页: https://alphapai-web.rabyte.cn/reading/home/market-report/detail\n"
        "\n"
        "沪指低开高走，成交额1.2万亿。\n\nAI算力链集体走强，关注：\n\n中际旭创、新易盛\n\n免责声明：本文不构成投资建议\n"
    )


def test_alphapai_filename_sanitize_unchanged(tmp_path):
    """内联快照：特殊字符标题的文件名（改造前实测）。"""
    path = Path(
        md_store.write_markdown_file(
            tmp_path,
            title='全球蓝宝书 9月20日晚间版 | 美联储: 降息? 50bp/25bp "悬念" <test> *star* 尾部.',
            published="2026/09/20 21:03",
            source_line=alphapai_feed.SOURCE_LINE,
            page_url=alphapai_feed.BLUEBOOK_URL,
            body_text="正文",
        )
    )

    assert path.name == "2026-09-20_全球蓝宝书 9月20日晚间版 - 美联储 - 降息- 50bp-25bp -悬念- -test- -star- 尾部_b4a76f950ab0.md"
    assert path.read_text(encoding="utf-8").startswith("# 全球蓝宝书 9月20日晚间版 | 美联储")
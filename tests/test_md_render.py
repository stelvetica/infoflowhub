from __future__ import annotations

from web.services import md_render

SAMPLE = """# 标题

## 小标题

- 项

| a | b |
| --- | --- |
| 1 | 2 |

**粗** `码` [t](u)
"""


def test_render_supports_report_subset():
    html = md_render.render(SAMPLE)
    assert "<h1>标题</h1>" in html
    assert "<h2>小标题</h2>" in html
    assert "<li>项</li>" in html
    assert "<table>" in html
    assert "<th>a</th>" in html and "<td>1</td>" in html
    assert "<strong>粗</strong>" in html
    assert "<code>码</code>" in html
    assert '<a href="u">t</a>' in html


def test_render_escapes_raw_html():
    html = md_render.render("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_render_supports_ordered_list_and_paragraph_break():
    html = md_render.render("1. 第一\n2. 第二\n\n下一段")
    assert "<ol><li>第一</li><li>第二</li></ol>" in html
    assert "<p>下一段</p>" in html


def test_render_restores_details_block():
    """产物里的折叠块要还原成真标签，不能显示成 &lt;details&gt; 文本。"""
    html = md_render.render(
        "<details><summary>产品A：近一周净 -1.2%</summary>\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n</details>"
    )
    assert "<details><summary>产品A：近一周净 -1.2%</summary>" in html
    assert "</details>" in html
    assert "&lt;details&gt;" not in html
    assert "<table>" in html


def test_render_keeps_details_with_attributes_escaped():
    """只认固定字面量：带属性的 details 仍按文本转义，避免成为注入入口。"""
    html = md_render.render("<details open><summary>x</summary></details>")
    assert "<details>" not in html
    assert "&lt;details open&gt;" in html


def test_render_downgrades_unsafe_link_scheme():
    html = md_render.render("[点我](javascript:alert(1))")
    assert "<a href" not in html


def test_render_empty_input():
    assert md_render.render("") == ""
"""极简 Markdown → HTML 渲染器（供投研产物可读页使用）。

ponytail: 只实现产物实际用到的语法子集——ATX 标题、无序/有序列表、单行管道表、
**加粗**、行内码、[文本](url)、空行分段。天花板：不支持嵌套列表、代码围栏与高亮、
引用块、图片、脚注、表格合并单元格与跨行表格；产物一旦用到这些语法会退化为纯文本。
升级路径：换 mistune（`render = mistune.html`，或在本函数内改为调用 mistune），
调用方（web/routes/main.py）无需改动。

安全：先整段 markupsafe.escape 再拼标签，因此原文里的 HTML 一律按文本显示；
`javascript:`/`data:` 等协议链接降级为纯文本，避免产物内的链接成为 XSS 入口。
"""

from __future__ import annotations

import re

from markupsafe import escape

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UNORDERED_RE = re.compile(r"^[-*]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_DIVIDER_CELL_RE = re.compile(r"^:?-{1,}:?$")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
_UNSAFE_URL_RE = re.compile(r"^\s*(?:javascript|data|vbscript):", re.IGNORECASE)
# 产物里的折叠块（<details><summary>…</summary>）是固定字面量：整段 escape 后按精确模式还原，
# 只认这两行且不带任何属性，其余 HTML 仍照旧转义为文本。
_DETAILS_OPEN_RE = re.compile(r"^&lt;details&gt;&lt;summary&gt;(.*)&lt;/summary&gt;$")
_DETAILS_CLOSE = "&lt;/details&gt;"


def _link(match: re.Match[str]) -> str:
    label, url = match.group(1), match.group(2)
    if _UNSAFE_URL_RE.match(url):
        return match.group(0)
    return f'<a href="{url}">{label}</a>'


def _inline(text: str) -> str:
    """行内语法：行内码优先（码内不再解析其它语法），其余段处理链接与加粗。"""
    parts = _CODE_SPAN_RE.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = _BOLD_RE.sub(r"<strong>\1</strong>", _LINK_RE.sub(_link, parts[index]))
    return "".join(f"<code>{part}</code>" if index % 2 else part for index, part in enumerate(parts))


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_table_start(lines: list[str], index: int) -> bool:
    """当前行含 `|` 且下一行是 `---` 分隔行时，判定为管道表起始。"""
    if "|" not in lines[index] or index + 1 >= len(lines):
        return False
    if "|" not in lines[index + 1]:
        return False
    cells = _split_row(lines[index + 1])
    return bool(cells) and all(_DIVIDER_CELL_RE.match(cell) for cell in cells)


def _render_table(rows: list[list[str]]) -> str:
    header, *body = rows
    width = len(header)
    head_html = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in (row + [""] * width)[:width]) + "</tr>"
        for row in body
    )
    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"


def render(markdown_text: str) -> str:
    """把 markdown 子集渲染为 HTML 片段（内容已转义，调用方用 |safe 输出）。"""
    lines = escape(markdown_text or "").splitlines()
    blocks: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    list_tag = ""

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_tag and items:
            body = "".join(f"<li>{item}</li>" for item in items)
            blocks.append(f"<{list_tag}>{body}</{list_tag}>")
        list_tag = ""
        items.clear()

    index = 0
    while index < len(lines):
        text = lines[index].strip()
        if not text:
            flush_paragraph()
            flush_list()
            index += 1
            continue

        heading = _HEADING_RE.match(text)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        details = _DETAILS_OPEN_RE.match(text)
        if details:
            flush_paragraph()
            flush_list()
            blocks.append(f"<details><summary>{_inline(details.group(1))}</summary>")
            index += 1
            continue

        if text == _DETAILS_CLOSE:
            flush_paragraph()
            flush_list()
            blocks.append("</details>")
            index += 1
            continue

        if _is_table_start(lines, index):
            flush_paragraph()
            flush_list()
            rows = [_split_row(lines[index])]
            index += 2  # 跳过分隔行
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                rows.append(_split_row(lines[index]))
                index += 1
            blocks.append(_render_table(rows))
            continue

        unordered = _UNORDERED_RE.match(text)
        ordered = _ORDERED_RE.match(text)
        if unordered or ordered:
            flush_paragraph()
            tag = "ul" if unordered else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append(_inline((unordered or ordered).group(1).strip()))
            index += 1
            continue

        flush_list()
        paragraph.append(_inline(text))
        index += 1

    flush_paragraph()
    flush_list()
    return "\n".join(blocks)
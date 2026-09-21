"""markdown 正文落盘的共享工具（Alpha派蓝宝书 / 韭研公社正文共用同一份格式）。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from connectors._shared.common import clean_line


def sanitize_filename(value: str) -> str:
    """清理文件名片段：折叠空白、替换非法字符、去首尾点、截断 120 字符。"""
    text = clean_line(value).replace("/", "-").replace("\\", "-").replace(":", " -")
    text = re.sub(r'[<>:"/\\|?*]+', "-", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    # 空标题兜底值沿用蓝宝书历史文件名格式（两个调用方的标题都非空，实际不可达）
    return text[:120] or "alphapai"


def write_markdown_file(
    directory: Path,
    *,
    title: str,
    published: str,
    source_line: str,
    page_url: str,
    body_text: str,
    extra_key: str = "",
    display_title: str = "",
    filename_prefix: str = "",
) -> str:
    """写一份正文 markdown，返回绝对路径字符串。

    文件名：`{published 前 10 位(斜杠转横杠)}_{filename_prefix}{净化标题}_{摘要}.md`
    摘要：`sha1(f"{published}|{title}" + (f"|{extra_key}" if extra_key else ""))[:12]`
    （extra_key 用于标题会重复的源，如韭研公社的 article_id；为空时与蓝宝书历史摘要
    输入 `{published}|{title}` 逐字节一致）。
    正文头四行：`# {标题}` / 发布时间 / 来源 / 原始页，其后接 `body_text`。

    display_title：正文首行标题，默认取 title（蓝宝书的正文标题与文件名标题历史上不同源）。
    filename_prefix：文件名标题段前缀（韭研公社用它带源名，蓝宝书为空）。
    """
    directory.mkdir(parents=True, exist_ok=True)
    heading = clean_line(display_title) or clean_line(title)
    digest_key = f"{published}|{title}" + (f"|{extra_key}" if extra_key else "")
    digest = hashlib.sha1(digest_key.encode("utf-8")).hexdigest()[:12]
    filename = f"{published[:10].replace('/', '-')}_{filename_prefix}{sanitize_filename(title)}_{digest}.md"
    path = directory / filename
    path.write_text(
        f"# {heading}\n\n"
        f"- 发布时间: {published}\n"
        f"- 来源: {source_line}\n"
        f"- 原始页: {page_url}\n\n"
        f"{body_text.strip()}\n",
        encoding="utf-8",
    )
    return str(path)
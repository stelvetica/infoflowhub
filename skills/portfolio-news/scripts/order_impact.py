"""《新闻影响分析》事件重排：有池内标的的事件在前，无池内标的的在后。

职责（确定性）：排序与编号重排是机械动作，脚本做；「哪些事件有池内标的」由阶段三
AI 判定并写进 `- 池内受影响标的：` 行，脚本只读取该行，不改写任何分析内容。

用法：python order_impact.py <新闻影响分析_YYYY-MM-DD.md>
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

EVENT_RE = re.compile(r"^##\s*事件\s*\d*\s*[：:]\s*(.*)$")
TARGET_LINE_RE = re.compile(r"^-\s*池内受影响标的\s*[：:]\s*(.*)$")
INDENT_BULLET_RE = re.compile(r"^\s{2,}[-*]\s+\S")
TOP_BULLET_RE = re.compile(r"^[-*]\s+")
# 阴性写法（「无」「池内无」「暂无」…）；「无锡银行」这类真标的不算，必须后接分隔符或结束
NEGATIVE_RE = re.compile(
    r"^(?:无|暂无|池内无|无标的|无相关|无影响|N/?A|—|-)(?:[（(、，,。.；;：:].*)?$",
    re.IGNORECASE,
)


def split_events(text: str) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    """拆成 (事件前内容, [(标题, 事件正文行)], 事件后的非事件 `##` 尾部)。

    尾部（如免责声明/数据来源）单独保留并放在文档末尾，避免被最后一个事件带进文档中部。
    """
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if EVENT_RE.match(line)), None)
    if first is None:
        return lines, [], []
    events: list[list] = []
    footer: list[str] = []
    for index, line in enumerate(lines[first:], start=first):
        match = EVENT_RE.match(line)
        if match:
            events.append([match.group(1).strip(), []])
        elif line.startswith("## "):
            footer = lines[index:]
            break
        else:
            events[-1][1].append(line)
    return lines[:first], [(title, body) for title, body in events], footer


def has_targets(body: list[str]) -> bool:
    """事件是否列了池内受影响标的（「无」「池内无」「无（…）」均算无）。

    有冒号后内容时以该内容为准（不再看缩进 bullet，避免「无」+ 备注被误判）；
    冒号后为空时才看紧跟的缩进 bullet。
    """
    for i, line in enumerate(body):
        match = TARGET_LINE_RE.match(line)
        if not match:
            continue
        rest = match.group(1).strip()
        if rest:
            return not NEGATIVE_RE.match(rest)
        for nxt in body[i + 1:]:
            if TOP_BULLET_RE.match(nxt):
                break
            if INDENT_BULLET_RE.match(nxt):
                return True
        return False
    return False


def reorder(text: str) -> tuple[str, int, int, list[str]]:
    """稳定分区重排并连续重编号；返回 (新文本, 有标数, 无标数, 缺标的行的事件标题)。"""
    preamble, events, footer = split_events(text)
    if not events:
        return text, 0, 0, []
    missing = [
        title for title, body in events
        if not any(TARGET_LINE_RE.match(line) for line in body)
    ]
    ranked = [(has_targets(body), title, body) for title, body in events]
    with_targets = [event for event in ranked if event[0]]
    without_targets = [event for event in ranked if not event[0]]
    out = list(preamble)
    for number, (_, title, body) in enumerate(with_targets + without_targets, start=1):
        out.append(f"## 事件 {number}：{title}")
        out.extend(body)
    if footer:
        if out and out[-1].strip():
            out.append("")
        out.extend(footer)
    return "\n".join(out).rstrip() + "\n", len(with_targets), len(without_targets), missing


SELFTEST = """# 新闻影响分析 2026-10-22

口径：上个交易日（2026-10-21）15:00 收盘后发布的新闻。

## 事件 1：无标的 A
- 影响性质：中性
- 池内受影响标的：无

## 事件 2：有标的 B
- 影响性质：利好
- 池内受影响标的：
  - `600000.SH` 浦发银行（示例）利好 中

## 事件 3：无标的带说明 C
- 池内受影响标的：无（池内无相关标的）

## 事件 4：有标的 D
- 池内受影响标的：
  - `000001.SZ` 平安银行（示例）利空 弱

## 事件 5：池内无写法 E
- 池内受影响标的：池内无

## 事件 6：暂无写法 F
- 池内受影响标的：暂无

## 事件 7：无带缩进备注 G
- 池内受影响标的：无
  - 备注：这条只是说明

## 事件 8：名称以无开头 H
- 池内受影响标的：无锡银行（示例）利好 强

## 事件 9：缺标的行 I
- 影响性质：中性

## 事件 10：尾部前 J
- 池内受影响标的：
  - `600036.SH` 招商银行（示例）利好 中

## 数据来源
本文数据来自公开资讯。
"""


def run_selftest() -> int:
    out, with_targets, without_targets, missing = reorder(SELFTEST)
    titles = [title for _, title in re.findall(r"^## 事件 (\d+)：(.+)$", out, flags=re.MULTILINE)]
    checks = [
        ("计数：4 有标 / 6 无标", (with_targets, without_targets) == (4, 6)),
        ("顺序：有标的 B、D、H、J 在前", titles[:4] == [
            "有标的 B", "有标的 D", "名称以无开头 H", "尾部前 J",
        ]),
        ("编号：从 1 连续", re.findall(r"^## 事件 (\d+)：", out, flags=re.MULTILINE) == [str(i) for i in range(1, 11)]),
        ("阴性：池内无/暂无/无带备注均排后", titles[4:] == [
            "无标的 A", "无标的带说明 C", "池内无写法 E", "暂无写法 F", "无带缩进备注 G", "缺标的行 I",
        ]),
        ("缺行：点名 I 但不中断", missing == ["缺标的行 I"]),
        ("内容：标的信息原样保留", "`600000.SH` 浦发银行" in out and "`000001.SZ` 平安银行" in out),
        ("尾部：非事件小节留在文末", out.rstrip().endswith("本文数据来自公开资讯。")),
        ("前言：原样保留", out.startswith("# 新闻影响分析 2026-10-22\n\n口径：")),
        ("边界：无事件时原样返回", reorder("# 标题\n\n正文\n") == ("# 标题\n\n正文\n", 0, 0, [])),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"selftest {'FAIL：' + '；'.join(failed) if failed else 'PASS'}")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="有池内标的的事件排前，无池内标的的排后，并重编号")
    parser.add_argument("path", nargs="?", type=Path, help="新闻影响分析 MD 路径（原地重写）")
    parser.add_argument("--selftest", action="store_true", help="只跑内置夹具自检（离线）")
    args = parser.parse_args()
    if args.selftest:
        raise SystemExit(run_selftest())
    if not args.path:
        parser.error("需要传入新闻影响分析 MD 路径")
    text = args.path.read_text(encoding="utf-8")
    out, with_targets, without_targets, missing = reorder(text)
    if (with_targets, without_targets) == (0, 0):
        print(f"未找到 `## 事件 N：` 小节，未改动：{args.path}")
        return
    args.path.write_text(out, encoding="utf-8")
    print(
        f"已重排：{args.path}（有池内标的 {with_targets} 个在前，"
        f"无池内标的 {without_targets} 个在后）"
    )
    if missing:
        print(
            f"警告：{len(missing)} 个事件缺少 `- 池内受影响标的：` 行，已按无标的处理："
            + "、".join(missing)
        )


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 infoflowhub 产物根的最新三类报告发布到观察目录（该目录只保留最新一套）。

归属 infoflowhub 仓库，由 portfolio-news / portfolio-announcements 两个 skill 共用。

- 复制：产物根下 `新闻影响分析_*.md` / `公告清单_*.md` / `公告清单_*.png` 各取最新 1 个。
- 清理：目标目录内**同类文件**的非最新者删除；其他文件（持仓标的.xlsx、自选股.xlsx、
  2025十大持仓\\ 等）一律不碰——删除只按上述三个 glob 模式匹配，不碰任何未匹配文件。
- 源缺某类 → 该类跳过（不复制也不清理），避免产物缺失时误删目标历史。
- 排序主键 = 文件名内日期，次键 = mtime：同步工具重落盘会刷新 mtime，日期才是可靠信号。

用法：
  python publish_reports.py                # 发布（复制最新 + 清理旧同类）
  python publish_reports.py --dry-run      # 只打印将要做的事
  python publish_reports.py --selftest     # 临时夹具自检（改脚本后先跑）
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

# 产物根 = 主 checkout 的 infoflowhub（数据与运行实例都在主 checkout，不是 worktree）
DEFAULT_SRC = Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports")
DEFAULT_TARGET = Path(r"C:\Users\ETT\Playground\infoflowhub\data\标的观察")
# 三类产物：glob 模式 → 人读名称
KINDS = (
    ("新闻影响分析_*.md", "新闻影响分析 md"),
    ("公告清单_*.md", "公告清单 md"),
    ("公告清单_*.png", "公告清单 png"),
)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def sort_key(path: Path) -> tuple[str, float]:
    """最新判定：文件名内日期（ISO 串可直接比较）优先，无日期则退回 mtime。"""
    m = DATE_RE.search(path.name)
    return (m.group(1) if m else "", path.stat().st_mtime)


def publish(src: Path, target: Path, dry_run: bool = False) -> list[str]:
    """各类取 src 最新 1 个复制到 target，并把 target 内同类旧文件删掉；返回动作日志。"""
    log: list[str] = []
    for pattern, label in KINDS:
        files = sorted(src.glob(pattern), key=sort_key, reverse=True) if src.is_dir() else []
        if not files:
            log.append(f"[跳过] 源缺 {label}（{src}\\{pattern}），目标不动")
            continue
        latest = files[0]
        if dry_run:
            log.append(f"[复制] {latest} -> {target}\\{latest.name}")
        else:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(latest, target / latest.name)
            log.append(f"[复制] {latest.name} -> {target}\\{latest.name}")
        # 只保留最新 1 个：目标内同类文件除最新者外全删（新复制件若被更“新”的旧文件
        # 压过也会一并删掉——「只保留最新一套」的应有语义）
        for old in sorted(target.glob(pattern), key=sort_key, reverse=True)[1:]:
            if dry_run:
                log.append(f"[删除] {old}")
            else:
                old.unlink()
                log.append(f"[删除] {old.name}")
    return log


def selftest() -> None:
    """临时夹具：复制最新 + 删除旧同类 + 不误删无关文件 + dry-run 不落盘。"""
    assert DEFAULT_SRC == Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports")
    assert "infoflowhub-reports" not in str(DEFAULT_SRC)  # 产物根是主 checkout，不是 worktree
    assert DEFAULT_TARGET == Path(r"C:\Users\ETT\Playground\infoflowhub\data\标的观察")

    with tempfile.TemporaryDirectory() as tmp:
        src, target = Path(tmp) / "reports", Path(tmp) / "观察"
        src.mkdir()
        target.mkdir()

        for name in ("新闻影响分析_2026-09-18.md", "新闻影响分析_2026-09-21.md",
                     "公告清单_2026-09-18.md", "公告清单_2026-09-21.md",
                     "公告清单_2026-09-18.png", "公告清单_2026-09-21.png"):
            (src / name).write_text("new", encoding="utf-8")
        old_kind = ("新闻影响分析_2026-09-17.md", "新闻影响分析_2026-09-16.md",
                    "公告清单_2026-09-17.md", "公告清单_2026-09-17.png")
        for name in old_kind:
            (target / name).write_text("old", encoding="utf-8")
        keepers = ("持仓标的.xlsx", "自选股.xlsx", "备注.txt", "公告清单备份.md")
        for name in keepers:
            (target / name).write_text("keep", encoding="utf-8")
        (target / "2025十大持仓").mkdir()
        (target / "2025十大持仓" / "刘总 十大赚钱 2025.png").write_text("keep", encoding="utf-8")

        log = publish(src, target)
        assert len(log) == len(KINDS) + len(old_kind), log

        names = {p.name for p in target.iterdir()}
        for newest in ("新闻影响分析_2026-09-21.md", "公告清单_2026-09-21.md",
                       "公告清单_2026-09-21.png"):
            assert newest in names, f"未复制最新：{newest}"
        assert "新闻影响分析_2026-09-18.md" not in names, "非最新源不应复制"
        for gone in old_kind:
            assert gone not in names, f"旧同类未清理：{gone}"
        for name in keepers:
            assert name in names, f"误删无关文件：{name}"
        assert (target / "2025十大持仓" / "刘总 十大赚钱 2025.png").exists(), "误删无关子目录"

        # dry-run 不落盘
        (src / "新闻影响分析_2026-09-22.md").write_text("newer", encoding="utf-8")
        before = {p.name for p in target.iterdir()}
        publish(src, target, dry_run=True)
        assert {p.name for p in target.iterdir()} == before, "dry-run 不应落盘"

        # 出现更新日期时只保留它（旧的最新套被清理）
        publish(src, target)
        names = {p.name for p in target.iterdir()}
        assert "新闻影响分析_2026-09-22.md" in names and "新闻影响分析_2026-09-21.md" not in names

        # 源缺某类 → 该类跳过，目标内旧文件不动
        src2 = Path(tmp) / "empty"
        src2.mkdir()
        log = publish(src2, target)
        assert all(l.startswith("[跳过]") for l in log), log
        assert "公告清单_2026-09-21.md" in {p.name for p in target.iterdir()}

    print("selftest PASS")


def main() -> None:
    ap = argparse.ArgumentParser(description="发布最新三类报告到观察目录（只保留最新一套）")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="产物根（默认 infoflowhub\\data\\reports）")
    ap.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="发布目标目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做的事，不落盘")
    ap.add_argument("--selftest", action="store_true", help="临时夹具自检")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    if not args.src.is_dir():
        print(f"[警告] 产物根不存在：{args.src}，未发布任何文件", file=sys.stderr)
        return

    mode = "（dry-run，不落盘）" if args.dry_run else ""
    print(f"发布：{args.src} -> {args.target}{mode}")
    for line in publish(args.src, args.target, dry_run=args.dry_run):
        print("  " + line)


if __name__ == "__main__":
    main()
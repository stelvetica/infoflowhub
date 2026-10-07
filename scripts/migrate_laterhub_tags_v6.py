"""laterhub 标签体系 v5 → v6 一次性历史数据迁移（纯确定性）。

v5→v6 是 5 处合并/改名：技术三细分并成「技术_AI/工具/开发」、健康+保养并成
「生活_健康/保养」、文学与综艺吃瓜改名/合并。全部有确定语义后继，没有需要
重判的歧义，故不调用任何模型、无并发、无关键词兜底。

用法：
    python scripts/migrate_laterhub_tags_v6.py [--db <sqlite 路径>]

- 不传 --db 时使用 apps.laterhub.config.DB_PATH 并打印该路径。
- 目标库不存在或没有 links 表时直接失败退出，绝不新建空库。
- 执行前用 SQLite 在线备份 API 取一致性快照 <db>.bak-<YYYYmmddHHMMSS>，撞名换名不覆盖，
  并做 PRAGMA integrity_check。
- 全程单事务，失败回滚；仅在 tags 真变化时写 updated_at。
- tags 为空/NULL 的行跳过（它们是待自动打标的 pending 行，写入标签会永久冻结）。
- 遇到映射表与 v4 白名单都覆盖不了的标签，整库拒绝迁移并报错（不猜、不丢）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from apps.laterhub.config import DB_PATH as DEFAULT_DB_PATH
from apps.laterhub.tagger import TAG_OPTIONS, TAG_WHITELIST

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # noqa: BLE001
    pass

# v3 名 → v4 名，显式穷举 30 项。刻意不用 `replace("-", "_")`：通用替换既不表达
# 「心理_认知/学习」这唯一一处语义改名，重跑时还会把已迁移名再次改写（不幂等）。
TAG_MAP: dict[str, str] = {
    "技术_AI": "技术_AI/工具/开发",
    "技术_工具": "技术_AI/工具/开发",
    "技术_开发": "技术_AI/工具/开发",
    "生活_健康": "生活_健康/保养",
    "生活_保养": "生活_健康/保养",
    "娱乐_文学/解说": "娱乐_文学/艺术",
    "娱乐_影视/游戏/动漫/解说": "娱乐_影视/游戏/动漫",
    "娱乐_综艺": "娱乐_综艺/吃瓜",
    "娱乐_吃瓜": "娱乐_综艺/吃瓜",
}

# 映射表必须与白名单自洽：键是 v3 旧名（不在 v4 白名单），值必须是 v4 合法名。
assert set(TAG_MAP.values()) <= TAG_WHITELIST and not (set(TAG_MAP) & TAG_WHITELIST), "TAG_MAP 与 v4 白名单不自洽"


def resolve_db_path(raw: str) -> Path:
    db_path = Path(raw) if raw else Path(DEFAULT_DB_PATH)
    print(f"[迁移] 目标数据库: {db_path}")
    if not db_path.exists() or not db_path.is_file():
        print(f"[迁移][失败] 目标库不存在，拒绝新建空库: {db_path}")
        raise SystemExit(1)
    return db_path


def assert_has_links_table(conn: sqlite3.Connection, db_path: Path) -> None:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='links'").fetchone()
    if not row:
        print(f"[迁移][失败] 目标库没有 links 表，拒绝迁移: {db_path}")
        raise SystemExit(1)


def backup_db(db_path: Path) -> Path:
    """用 SQLite 在线备份 API 取一致性快照；撞名换名不覆盖（备份是唯一退路）。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    suffix = 1
    while backup_path.exists():
        backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(backup_path)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        print(f"[迁移][失败] 备份完整性校验未通过: {result}")
        raise SystemExit(1)
    print(f"[迁移] 已备份到: {backup_path}（完整性校验通过）")
    return backup_path


def resolve_tag(tag: str) -> str:
    """v3 名按 TAG_MAP 改名；已是 v4 名原样保留（幂等）；其余一律报错拒绝整库迁移。"""
    if tag in TAG_MAP:
        return TAG_MAP[tag]
    if tag in TAG_WHITELIST:
        return tag
    raise ValueError(f"标签不在迁移映射表或 v4 白名单内，拒绝迁移: {tag!r}")


def split_tags(raw: str) -> list[str]:
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def migrate(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT id, source, title, tags FROM links ORDER BY id ASC").fetchall()
        total_rows = len(rows)
        snapshot = {int(r["id"]): (str(r["source"]), str(r["title"])) for r in rows}
        before_counts: Counter = Counter()
        after_counts: Counter = Counter()
        skipped_empty = 0
        changed = 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        for row in rows:
            raw = row["tags"]
            if raw is None or not str(raw).strip():
                skipped_empty += 1
                continue
            parts = split_tags(str(raw))
            before_counts.update(parts)
            mapped: list[str] = []
            for part in parts:
                new_tag = resolve_tag(part)
                if new_tag not in mapped:
                    mapped.append(new_tag)
            mapped = mapped[:2]
            after_counts.update(mapped)
            new_tags = ",".join(mapped)
            if new_tags == str(raw):
                # 标签没变就不动 updated_at：它是 recover_stale_digest_runs 判断"简报任务
                # 卡死"的唯一依据，全量刷新会把中断的任务推迟最多 2 小时才重新排队。
                continue
            conn.execute(
                "UPDATE links SET tags = ?, updated_at = ? WHERE id = ?",
                (new_tags, now, int(row["id"])),
            )
            changed += 1

        # 迁移后自校验（仍在事务内，失败即回滚）
        after_total = int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
        if after_total != total_rows:
            raise RuntimeError(f"自校验失败：行数变化 {total_rows} -> {after_total}")
        for row in conn.execute("SELECT id, source, title, tags FROM links ORDER BY id ASC").fetchall():
            link_id = int(row["id"])
            if snapshot.get(link_id) != (str(row["source"]), str(row["title"])):
                raise RuntimeError(f"自校验失败：source/title 发生变化 id={link_id}")
            raw = row["tags"]
            if raw is None or not str(raw).strip():
                continue
            for tag in split_tags(str(raw)):
                if tag not in TAG_WHITELIST:
                    raise RuntimeError(f"自校验失败：残留非白名单标签 {tag!r} id={link_id}")

        illegal = {tag: count for tag, count in after_counts.items() if tag not in TAG_WHITELIST}

        print("[迁移] 统计：")
        print(f"  总行数: {total_rows}")
        print(f"  空标签跳过: {skipped_empty}")
        print(f"  实际改写行数: {changed}")
        print("  迁移前标签分布（按出现次数）:")
        for tag, count in sorted(before_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {tag}: {count}")
        print("  迁移后标签分布（按 catalog 顺序）:")
        for tag in TAG_OPTIONS:
            print(f"    {tag}: {after_counts.get(tag, 0)}")
        print(f"  非法残留: {illegal if illegal else '无'}")

        conn.commit()
    except Exception:
        conn.rollback()
        print("[迁移][失败] 已回滚，数据库未改动")
        raise
    finally:
        conn.close()

    print("[迁移] 完成。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="laterhub 标签体系 v3 → v4 确定性历史数据迁移")
    parser.add_argument("--db", default="", help="目标 SQLite 路径；缺省用 apps.laterhub.config.DB_PATH")
    args = parser.parse_args(argv)
    db_path = resolve_db_path(args.db)
    probe = sqlite3.connect(db_path)
    try:
        assert_has_links_table(probe, db_path)
    finally:
        probe.close()
    backup_db(db_path)
    return migrate(db_path)


if __name__ == "__main__":
    raise SystemExit(main())

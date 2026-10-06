"""laterhub 标签体系 v1 → v2 一次性历史数据迁移。

用法：
    python scripts/migrate_laterhub_tags_v2.py --db <目标 sqlite 路径>

- 不传 --db 时使用 apps.laterhub.config.DB_PATH 并打印该路径。
- 目标库不存在或没有 links 表时直接失败退出，绝不新建空库。
- 执行前自动复制 <db>.bak-<YYYYmmddHHMMSS> 备份。
- 全程单事务，失败回滚。
- tags 为空/NULL 的行跳过（它们是待自动打标的 pending 行，写入标签会永久冻结）。
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
from apps.laterhub.tagger import DEFAULT_TAG, TAG_OPTIONS, TAG_WHITELIST

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # noqa: BLE001
    pass

# 旧标签 → 新标签；除「技术-音乐/唱歌」外一律 1:1（含未改名的自映射）。
OLD_TO_NEW: dict[str, str] = {
    "心理-认知": "心理-认知",
    "心理-自我/情绪": "心理-自我/情绪",
    "心理-他人/关系": "心理-他人/关系",
    "心理-职场": "心理-职场",
    "技术-AI": "技术-AI",
    "技术-效率/工具/开发": "技术-效率/工具/开发",
    "技术-英语/写作": "技术-创作/表达",
    "技术-摄影/剪辑": "技术-创作/表达",
    "技术-科学/科普": "技术-科学/科普",
    "社科-金融/商业/经济": "社科-金融/商业/消费",
    "社科-历史": "社科-历史",
    "社科-社会/时政": "社科-社会/时政",
    "人文-哲学/艺术": "文化-哲学/文学/艺术/音乐",
    "人文-玄学": "文化-玄学",
    "生活-健康/健身": "生活-健康/健身",
    "生活-穿搭/保养": "生活-穿搭/保养",
    "生活-美食/旅行": "生活-美食/旅行",
    "娱乐-影视/游戏/动漫": "娱乐-影视/游戏/动漫",
    "娱乐-综艺/吃瓜": "娱乐-综艺/吃瓜",
    "解说-文学": "文化-哲学/文学/艺术/音乐",
    "解说-影视/游戏/动漫": "娱乐-影视/游戏/动漫",
    "解说-综艺/吃瓜": "娱乐-综艺/吃瓜",
}

MUSIC_OLD = "技术-音乐/唱歌"
MUSIC_TEACHING_WORDS = ["教唱", "教学", "教程", "发声", "声乐", "唱法", "混声", "乐理", "练习", "训练"]


def split_tags(raw: str) -> list[str]:
    text = (
        raw.replace("，", ",")
        .replace("、", ",")
        .replace("；", ",")
        .replace("：", ",")
        .replace(":", ",")
        .replace("！", ",")
        .replace("!", ",")
    )
    return [part.strip() for part in text.split(",") if part.strip()]


def map_tag(tag: str, title: str) -> str | None:
    if tag == MUSIC_OLD:
        return "技术-创作/表达" if any(word in title for word in MUSIC_TEACHING_WORDS) else "文化-哲学/文学/艺术/音乐"
    if tag in TAG_WHITELIST:
        return tag
    return OLD_TO_NEW.get(tag)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="laterhub 标签体系 v2 历史数据迁移")
    parser.add_argument("--db", default="", help="目标 SQLite 路径；缺省用 apps.laterhub.config.DB_PATH")
    return parser.parse_args(argv)


def resolve_db_path(raw: str) -> Path:
    db_path = Path(raw) if raw else Path(DEFAULT_DB_PATH)
    print(f"[迁移] 目标数据库: {db_path}")
    if not db_path.exists():
        print(f"[迁移][失败] 目标库不存在，拒绝新建空库: {db_path}")
        raise SystemExit(1)
    if not db_path.is_file():
        print(f"[迁移][失败] 目标不是文件: {db_path}")
        raise SystemExit(1)
    return db_path


def assert_has_links_table(conn: sqlite3.Connection, db_path: Path) -> None:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='links'").fetchone()
    if not row:
        print(f"[迁移][失败] 目标库没有 links 表，拒绝迁移: {db_path}")
        raise SystemExit(1)


def backup_db(db_path: Path) -> Path:
    """用 SQLite 在线备份 API 取一致性快照。

    直接 shutil.copy 文件在并发写入时会复制到"半提交"状态（回滚日志不随文件一起复制），
    备份会静默损坏——而备份是本次迁移唯一的退路，必须保证可读。
    """
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    # 同一秒内二次执行（含误重跑）会撞名，而 old 备份是本次迁移唯一的退路：
    # 撞名必须换名而不是覆盖，否则退路被悄悄换成"已迁移"状态。
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = resolve_db_path(args.db)
    probe = sqlite3.connect(db_path)
    try:
        assert_has_links_table(probe, db_path)
    finally:
        probe.close()
    backup_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")

        rows = conn.execute("SELECT id, url, title, tags FROM links ORDER BY id ASC").fetchall()
        total_rows = len(rows)
        snapshot = {int(r["id"]): (r["url"], r["title"]) for r in rows}
        before_counts: Counter = Counter()
        before_two_tag = 0

        skipped_empty = 0
        dedupe_hits = 0
        truncated = 0
        unknown_tags: set[str] = set()
        updates: list[tuple[str, int]] = []
        after_counts: Counter = Counter()
        after_two_tag = 0

        for row in rows:
            raw = row["tags"]
            if raw is None or not str(raw).strip():
                skipped_empty += 1
                continue
            parts = split_tags(str(raw))
            for part in parts:
                before_counts[part] += 1
            if len(parts) >= 2:
                before_two_tag += 1

            mapped: list[str] = []
            for part in parts:
                new_tag = map_tag(part, str(row["title"] or ""))
                if new_tag is None:
                    unknown_tags.add(part)
                    continue
                mapped.append(new_tag)
            deduped: list[str] = []
            for tag in mapped:
                if tag not in deduped:
                    deduped.append(tag)
            dedupe_hits += len(mapped) - len(deduped)
            if len(deduped) > 2:
                truncated += len(deduped) - 2
                deduped = deduped[:2]
            if not deduped:
                # 理论上不会发生；保守兜底不写入
                unknown_tags.update(parts)
                continue
            if len(deduped) == 2:
                after_two_tag += 1
            for tag in deduped:
                after_counts[tag] += 1
            updates.append((",".join(deduped), int(row["id"]), ",".join(parts)))

        if unknown_tags:
            raise RuntimeError(f"存在映射表未覆盖的标签，拒绝迁移以免丢数据: {sorted(unknown_tags)}")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for new_tags, link_id, old_tags in updates:
            if new_tags == old_tags:
                # 标签没变就不动 updated_at：它是 recover_stale_digest_runs 判断"简报任务
                # 卡死"的唯一依据，全量刷新会把中断的任务推迟最多 2 小时才重新排队。
                continue
            conn.execute(
                "UPDATE links SET tags = ?, updated_at = ? WHERE id = ?",
                (new_tags, now, link_id),
            )

        # 迁移后自校验（仍在事务内，失败即回滚）
        after_total = int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
        if after_total != total_rows:
            raise RuntimeError(f"自校验失败：行数变化 {total_rows} -> {after_total}")
        for row in conn.execute("SELECT id, url, title, tags FROM links ORDER BY id ASC").fetchall():
            link_id = int(row["id"])
            if snapshot.get(link_id) != (row["url"], row["title"]):
                raise RuntimeError(f"自校验失败：url/title 发生变化 id={link_id}")
            raw = row["tags"]
            if raw is None or not str(raw).strip():
                continue
            for tag in split_tags(str(raw)):
                if tag not in TAG_WHITELIST:
                    raise RuntimeError(f"自校验失败：残留非白名单标签 {tag!r} id={link_id}")
        if len(snapshot) != total_rows:
            raise RuntimeError("自校验失败：行数变化")
        conn.commit()
    except Exception:
        conn.rollback()
        print("[迁移][失败] 已回滚，数据库未改动")
        raise
    finally:
        conn.close()

    print("[迁移] 完成。")
    print(f"  总行数: {total_rows}")
    print(f"  空标签跳过: {skipped_empty}")
    print(f"  去重命中(合并掉的标签项): {dedupe_hits}")
    print(f"  超出 2 项被截断: {truncated}")
    print(f"  迁移后双标签行: {after_two_tag}")
    print(f"  迁移前双标签行: {before_two_tag}")
    print("  迁移前标签出现次数:")
    for tag, count in before_counts.most_common():
        print(f"    {tag}: {count}")
    print("  迁移后标签出现次数:")
    for tag in TAG_OPTIONS:
        print(f"    {tag}: {after_counts.get(tag, 0)}")
    for tag, count in after_counts.items():
        if tag not in TAG_WHITELIST:
            print(f"    [非法]{tag}: {count}")
    print(f"  迁移后 DEFAULT_TAG({DEFAULT_TAG}) 出现: {after_counts.get(DEFAULT_TAG, 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""laterhub 可复用「重打标签」工具：按前缀圈定目标行，用当前提示词让 LLM 重判并写回。

**这不是幂等迁移脚本**：它会真实调用 LLM，重跑会把上一次的重判结果再覆盖一遍，
每次运行都会消耗模型额度并改写 updated_at。

用法：
    python scripts/retag_laterhub_entries.py [--db <sqlite 路径>] [--workers 6] [--allow-degraded]

目标行 = tags 中任一项前缀属于 {心理_, 技术_, 社科_} 的行，**或** tags 为空/NULL 的行；
其余行（只含生活_*、娱乐_* 或其他未圈定标签）一律不动。空标签行也纳入是为了让这批
pending 行在重判中一并拿到标签。

护栏（沿用 scripts/migrate_laterhub_tags_v4.py 的口径）：
- 目标库不存在或没有 links 表即失败退出，绝不新建空库。
- 执行前用 SQLite 在线备份 API（非裸拷）取一致性快照 <db>.bak-<时间戳> 并做 integrity_check，
  撞名换名不覆盖（-1/-2…）。
- 全程单事务，失败回滚；仅在 tags 真变化时写 updated_at（它是卡死简报任务的判据）。

模型调用：ContentTagger（主 + 备 provider，从 <项目根>/.env 读），每行最多 3 次尝试
（首次 + 重试 2 次，每次内部已含主备切换）。并发只用于模型调用，所有 SQLite 写入回到
主线程串行执行。

失败兜底：模型 3 次都失败时，用确定性改名表把该行现有标签逐项映射成 v5 名，映射不上的
项落「其他_无法判断」。任何情况下都不写空 tags、不写白名单外的串。
模型失败率 > 10% 且未加 --allow-degraded 时回滚退出（码 2）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from apps.laterhub.config import DB_PATH as DEFAULT_DB_PATH
from apps.laterhub.tagger import (
    DEFAULT_TAG,
    TAG_OPTIONS,
    TAG_WHITELIST,
    ContentTagger,
    LLMConfig,
    normalize_tag,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # noqa: BLE001
    pass

TARGET_PREFIXES = ("心理_", "技术_", "社科_")
MAX_ATTEMPTS_PER_ROW = 3
FAILURE_RATE_LIMIT = 0.10
ENV_PATH = BASE_DIR / ".env"

# v4 名 → v5 名的确定性兜底映射（仅模型失败时使用）。显式穷举，不用通用替换：
# 合并类（社科_哲学/社会/经济/金融、社科_艺术/音乐）与改名类（社科_玄学→社科_东哲）
# 都不是机械替换能表达的。值必须全部落在 v5 白名单内。
RENAME_MAP: dict[str, str] = {
    "心理_认知/学习": "心理_认知/学习方法",
    "心理_自我/情绪": "心理_自我/情绪分析",
    "心理_他人/关系": "心理_他人/关系",
    "心理_职场": "心理_职场",
    "技术_AI": "技术_AI",
    "技术_英语": "技术_英语",
    "技术_摄影": "技术_摄影",
    "技术_唱歌": "技术_唱歌",
    "技术_写作": "技术_写作",
    "技术_工具": "技术_工具",
    "技术_开发": "技术_开发",
    "社科_科学/科普": "社科_科学/科普",
    "社科_历史": "社科_历史",
    "社科_哲学": "社科_哲学/社会/经济/金融",
    "社科_社会": "社科_哲学/社会/经济/金融",
    "社科_经济/金融": "社科_哲学/社会/经济/金融",
    "社科_艺术": "社科_艺术/音乐",
    "社科_音乐": "社科_艺术/音乐",
    "社科_玄学": "社科_东哲",
}
assert set(RENAME_MAP.values()) <= TAG_WHITELIST, "RENAME_MAP 映射目标必须全部在 v5 白名单内"


def split_tags(raw: object) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def is_target(raw_tags: object) -> bool:
    parts = split_tags(raw_tags)
    if not parts:
        return True
    return any(part.startswith(TARGET_PREFIXES) for part in parts)


def resolve_db_path(raw: str) -> Path:
    db_path = Path(raw) if raw else Path(DEFAULT_DB_PATH)
    print(f"[重打] 目标数据库: {db_path}")
    if not db_path.exists() or not db_path.is_file():
        print(f"[重打][失败] 目标库不存在，拒绝新建空库: {db_path}")
        raise SystemExit(1)
    return db_path


def assert_has_links_table(conn: sqlite3.Connection, db_path: Path) -> None:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='links'").fetchone()
    if not row:
        print(f"[重打][失败] 目标库没有 links 表，拒绝执行: {db_path}")
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
        print(f"[重打][失败] 备份完整性校验未通过: {result}")
        raise SystemExit(1)
    print(f"[重打] 已备份到: {backup_path}（完整性检查通过）")
    return backup_path


def load_tagger() -> ContentTagger:
    primary = LLMConfig.from_env(ENV_PATH)
    try:
        backup = LLMConfig.from_env(ENV_PATH, backup=True)
    except Exception:  # noqa: BLE001
        backup = None
    print(f"[重打] 模型配置: {primary.model}" + (f"（备用 {backup.model}）" if backup else "（无备用）"))
    return ContentTagger(primary, backup_config=backup)


def model_tags_for(tagger: ContentTagger, row: sqlite3.Row) -> tuple[list[str] | None, str]:
    """调用模型并归一化；返回 (标签列表, 失败原因)。列表为 None 表示这一行调用失败。"""
    title = str(row["title"] or "")
    source = str(row["source"] or "")
    last_error = "未知错误"
    for _ in range(MAX_ATTEMPTS_PER_ROW):
        try:
            raw = tagger.tag(title=title, source=source)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        parts = split_tags(raw)
        normalized = [normalize_tag(part) for part in parts]
        # 出现任何非法/废弃标签（归一化为 None）即视为这次调用失败，绝不把非法串写库。
        if not normalized or any(tag is None for tag in normalized) or not 1 <= len(normalized) <= 2:
            last_error = f"模型返回非法/废弃标签: {raw!r}"
            continue
        return [tag for tag in normalized if tag], ""
    return None, last_error


def deterministic_fallback(raw_tags: object) -> list[str]:
    """模型失败时用改名表兜底：逐项映射，映射不上落 DEFAULT_TAG；绝不产生空标签。"""
    mapped: list[str] = []
    for part in split_tags(raw_tags):
        new_tag = RENAME_MAP.get(part)
        if new_tag is None or new_tag not in TAG_WHITELIST:
            new_tag = DEFAULT_TAG
        if new_tag not in mapped:
            mapped.append(new_tag)
    if not mapped:
        mapped = [DEFAULT_TAG]
    return mapped[:2]


def retag_entries(db_path: Path, *, workers: int, allow_degraded: bool, tagger: ContentTagger) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, url, title, source, tags, tag_status FROM links ORDER BY id ASC").fetchall()
        total_rows = len(rows)
        snapshot = {int(r["id"]): (str(r["url"]), str(r["title"]), str(r["source"])) for r in rows}
        before_counts: Counter = Counter()
        targets: list[sqlite3.Row] = []
        for row in rows:
            if is_target(row["tags"]):
                targets.append(row)
                before_counts.update(split_tags(row["tags"]))

        print(f"[重打] 总行数 {total_rows}，目标行 {len(targets)}，不动行 {total_rows - len(targets)}")

        model_result: dict[int, list[str] | None] = {}
        model_error: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(model_tags_for, tagger, row): row for row in targets}
            for future in as_completed(futures):
                row = futures[future]
                tags, error = future.result()
                model_result[int(row["id"])] = tags
                model_error[int(row["id"])] = error

        conn.execute("BEGIN")
        after_counts: Counter = Counter()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        failed_rows: list[tuple[sqlite3.Row, str, list[str]]] = []
        model_success = 0
        fallback_count = 0
        changed = 0

        for row in targets:
            row_id = int(row["id"])
            tags = model_result.get(row_id)
            if tags:
                new_tags = tags
                model_success += 1
            else:
                new_tags = deterministic_fallback(row["tags"])
                fallback_count += 1
                failed_rows.append((row, model_error.get(row_id, "未知错误"), new_tags))
            assert 1 <= len(new_tags) <= 2, f"自校验失败：目标行标签数越界 id={row_id} 标签={new_tags}"
            assert all(tag in TAG_WHITELIST for tag in new_tags), f"自校验失败：非法标签 id={row_id} 标签={new_tags}"
            after_counts.update(new_tags)
            new_text = ",".join(new_tags)
            old_text = str(row["tags"] or "")
            if new_text == old_text:
                # 标签没变也要补齐状态：未打标行是 tag_status='pending'，重打成功后必须
                # 落为 done，否则留下"有标签却 pending"的中间态。
                if str(row["tag_status"] or "") != "done":
                    conn.execute(
                        "UPDATE links SET tag_status = 'done', tag_error_message = NULL, updated_at = ? WHERE id = ?",
                        (now, row_id),
                    )
                continue
            conn.execute(
                "UPDATE links SET tags = ?, tag_status = 'done', tag_error_message = NULL, updated_at = ? WHERE id = ?",
                (new_text, now, row_id),
            )
            changed += 1

        # 事务内自校验：失败即回滚
        after_total = int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
        if after_total != total_rows:
            raise RuntimeError(f"自校验失败：行数变化 {total_rows} -> {after_total}")
        target_ids = {int(r["id"]) for r in targets}
        for row in conn.execute("SELECT id, url, title, source, tags FROM links ORDER BY id ASC").fetchall():
            row_id = int(row["id"])
            if snapshot.get(row_id) != (str(row["url"]), str(row["title"]), str(row["source"])):
                raise RuntimeError(f"自校验失败：url/title/source 发生变化 id={row_id}")
            parts = split_tags(row["tags"])
            if row_id in target_ids:
                if not 1 <= len(parts) <= 2:
                    raise RuntimeError(f"自校验失败：目标行标签数不在 1~2 id={row_id} 标签={parts}")
            for tag in parts:
                if tag not in TAG_WHITELIST:
                    raise RuntimeError(f"自校验失败：残留非白名单标签 {tag!r} id={row_id}")

        failure_rate = len(failed_rows) / len(targets) if targets else 0.0
        if failed_rows:
            print(f"[重打] 失败明细（{len(failed_rows)} 行）:")
            for row, reason, final_tags in failed_rows:
                title = str(row["title"] or "")[:60]
                print(f"  id={row['id']} | {row['source']} | {title} | {reason} | 兜底={','.join(final_tags)}")

        print("[重打] 统计：")
        print(f"  总行数: {total_rows}")
        print(f"  目标行数: {len(targets)}")
        print(f"  未动行数: {total_rows - len(targets)}")
        print(f"  实际改写行数: {changed}")
        print(f"  模型成功数: {model_success}")
        print(f"  确定性兜底数: {fallback_count}")
        print(f"  落 {DEFAULT_TAG} 数: {after_counts.get(DEFAULT_TAG, 0)}")
        print(f"  失败率: {failure_rate:.2%}（阈值 {FAILURE_RATE_LIMIT:.0%}）")
        print("  重打前标签分布（按出现次数）:")
        for tag, count in sorted(before_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {tag}: {count}")
        print("  重打后标签分布（按 catalog 顺序）:")
        for tag in TAG_OPTIONS:
            print(f"    {tag}: {after_counts.get(tag, 0)}")
        illegal = {tag: count for tag, count in after_counts.items() if tag not in TAG_WHITELIST}
        print(f"  非法残留: {illegal if illegal else '无'}")

        if failure_rate > FAILURE_RATE_LIMIT and not allow_degraded:
            conn.rollback()
            print(
                f"[重打][失败] 模型失败率 {failure_rate:.2%} 超过 {FAILURE_RATE_LIMIT:.0%}，"
                "已回滚；确认可接受时加 --allow-degraded 重跑。"
            )
            return 2
        conn.commit()
    except Exception:
        conn.rollback()
        print("[重打][失败] 已回滚，数据库未改动")
        raise
    finally:
        conn.close()

    print("[重打] 完成。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="laterhub 可复用重打标签工具（真实调用 LLM，非幂等）")
    parser.add_argument("--db", default="", help="目标 SQLite 路径；缺省用 apps.laterhub.config.DB_PATH")
    parser.add_argument("--workers", type=int, default=6, help="模型调用并发数（默认 6）")
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="模型失败率超阈值时仍提交（默认回滚退出码 2）",
    )
    args = parser.parse_args(argv)
    db_path = resolve_db_path(args.db)
    probe = sqlite3.connect(db_path)
    try:
        assert_has_links_table(probe, db_path)
    finally:
        probe.close()
    tagger = load_tagger()
    backup_db(db_path)
    return retag_entries(db_path, workers=args.workers, allow_degraded=args.allow_degraded, tagger=tagger)


if __name__ == "__main__":
    raise SystemExit(main())

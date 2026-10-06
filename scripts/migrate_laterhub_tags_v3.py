"""laterhub 标签体系 v2 → v3 一次性历史数据迁移。

用法：
    python scripts/migrate_laterhub_tags_v3.py --db <目标 sqlite 路径> [--allow-degraded] [--workers 4]

- 不传 --db 时使用 apps.laterhub.config.DB_PATH 并打印该路径。
- 目标库不存在或没有 links 表时直接失败退出，绝不新建空库。
- 执行前用 SQLite 在线备份 API 取一致性快照 <db>.bak-<YYYYmmddHHMMSS>，撞名换名不覆盖，
  并做 PRAGMA integrity_check。
- 全程单事务，失败回滚；仅在 tags 真变化时写 updated_at。
- tags 为空/NULL 的行跳过（它们是待自动打标的 pending 行，写入标签会永久冻结）。

两条路径：
  A. 1:1 改名：一行内所有标签都在 PATH_A_MAP 里时直接改名写回，不调用模型。
  B. 交模型重判：一行含 PATH_B_TRIGGERS 任一标签（或含任何无法 1:1 映射的未知标签）时，
     整行用 v3 提示词以 source+title 重新判定（不把旧标签喂给模型，避免锚定）。
     模型失败时按「确定性关键词兜底 → 其他-无法判断」降级；模型失败率 > 10% 时需
     --allow-degraded 才允许提交，否则回滚。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from apps.laterhub.config import DB_PATH as DEFAULT_DB_PATH
from apps.laterhub.feishu import load_project_env
from apps.laterhub.tagger import (
    ContentTagger,
    DEFAULT_TAG,
    LLMConfig,
    TAG_OPTIONS,
    TAG_WHITELIST,
    normalize_tag,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # noqa: BLE001
    pass

ENV_PATH = BASE_DIR / ".env"
DEFAULT_WORKERS = 4
# 每行最多重试 2 次（首次 + 2 次重试）；每次 tag() 内部已含主/备 provider 的既有切换。
MAX_ATTEMPTS = 3
DEGRADED_FAILURE_RATE = 0.10

# 路径 A：1:1 改名（含未改名的自映射）。不调用模型。
PATH_A_MAP: dict[str, str] = {
    "心理-认知": "心理-认知",
    "心理-自我/情绪": "心理-自我/情绪",
    "心理-他人/关系": "心理-他人/关系",
    "心理-职场": "心理-职场",
    "技术-AI": "技术-AI",
    "社科-历史": "社科-历史",
    "技术-科学/科普": "社科-科学/科普",
    "社科-社会/时政": "社科-社会",
    "社科-金融/商业/消费": "社科-经济/金融",
    "文化-玄学": "社科-玄学",
    "娱乐-影视/游戏/动漫": "娱乐-影视/游戏/动漫/解说",
}

# 路径 B：出现下列任一标签即整行交模型重判。
PATH_B_TRIGGERS: frozenset[str] = frozenset(
    {
        "生活-健康/健身",
        "文化-哲学/文学/艺术/音乐",
        "技术-效率/工具/开发",
        "娱乐-综艺/吃瓜",
        "技术-创作/表达",
        "生活-美食/旅行",
        "生活-穿搭/保养",
    }
)

# 确定性关键词兜底表：每个关键词只出现在一个标签下（冲突已消解）。
KEYWORDS: dict[str, tuple[str, ...]] = {
    "技术-英语": ("英语", "英文", "口语", "发音", "单词", "词汇", "语法", "雅思", "托福", "四六级", "英式", "美式"),
    "技术-摄影": ("摄影", "拍摄", "镜头", "构图", "灯光", "修图", "后期", "剪辑", "拉片", "运镜"),
    "技术-唱歌": ("唱歌", "声乐", "发声", "气息", "混声", "音准", "音域", "教唱", "学唱歌", "唱功"),
    "技术-写作": ("写作", "表达", "文案", "文章结构", "写作方法", "读书方法", "表达能力"),
    "技术-工具": ("软件", "工具", "工作流", "效率", "自动化", "知识管理", "资料管理", "zotero", "笔记"),
    "技术-开发": ("编程", "代码", "数据库", "架构", "java", "python", "程序", "程序员", "开发", "转码", "debug"),
    "社科-哲学": ("哲学", "康德", "斯宾诺莎", "尼采", "存在主义", "形而上学", "纯粹理性批判", "先验"),
    "社科-艺术": ("艺术", "审美", "艺术史", "艺术理论", "绘画", "雕塑", "画作", "美学"),
    "社科-音乐": ("音乐", "歌曲", "神曲", "乐队", "专辑", "翻唱", "国摇", "民谣", "音乐史", "音乐现场"),
    "社科-玄学": ("命理", "占卜", "算命", "风水", "星座", "塔罗", "灵性", "修心", "悟人生", "易经", "六爻", "道家"),
    "生活-健康": ("养生", "营养", "睡眠", "疾病", "保健", "健康", "血糖", "血压", "体检", "长寿"),
    "生活-健身": ("健身", "锻炼", "训练", "平板支撑", "练肩", "深蹲", "体态", "塑形", "减脂", "居家锻炼", "核心力量", "肌肉"),
    "生活-穿搭": ("穿搭", "搭配", "衬衫", "裙子", "裤子", "通勤装", "配饰", "穿衣", "显瘦", "风格"),
    "生活-保养": ("护肤", "化妆", "护发", "发型", "变美", "保养", "抗老", "美容", "皮肤"),
    "生活-美食": ("美食", "探店", "餐馆", "菜品", "吃了", "逛吃", "餐厅", "菜谱", "做饭", "好吃", "味道"),
    "生活-旅行": ("旅行", "旅游", "景点", "路线", "住宿", "攻略", "出行", "目的地", "自驾", "民宿"),
    "娱乐-影视/游戏/动漫/解说": ("电影", "电视剧", "影视", "美剧", "动漫", "动画", "漫改", "剧情", "片单", "游戏", "二次元"),
    "娱乐-文学/解说": ("小说", "网文", "名著", "文学", "章节", "完结", "书单", "作者"),
    "娱乐-综艺": ("综艺", "脱口秀", "访谈", "真人秀", "喜人", "节目", "喜剧"),
    "娱乐-吃瓜": ("八卦", "恋情", "热搜", "明星", "女星", "塌房", "争议", "娱乐新闻"),
}

# 冲突裁决优先级（浮点在列表末尾）。除「生活-旅行/美食」的住宿/景点/路线条件外，
# 同标题命中多个标签时取本表中出现最靠前者。
PRIORITY: tuple[str, ...] = (
    "技术-唱歌",  # > 社科-音乐
    "技术-摄影",  # > 娱乐-影视/游戏/动漫/解说
    "技术-开发",  # > 技术-工具
    "技术-AI",  # > 技术-工具
    "技术-工具",
    "社科-音乐",
    "娱乐-影视/游戏/动漫/解说",
    "娱乐-文学/解说",  # > 技术-写作
    "技术-写作",
    "生活-旅行",
    "生活-美食",
    "娱乐-综艺",  # > 娱乐-吃瓜
    "娱乐-吃瓜",
    "生活-健身",
    "生活-健康",
    "生活-穿搭",
    "生活-保养",
    "技术-英语",
    "社科-哲学",
    "社科-艺术",
    "社科-玄学",
)
_PRIORITY_RANK = {tag: index for index, tag in enumerate(PRIORITY)}
TRAVEL_STRONG_WORDS = frozenset({"住宿", "景点", "路线"})


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


def keyword_fallback(title: str) -> str | None:
    """确定性关键词兜底：命中多个标签时按 PRIORITY 取第一个。

    垃圾标题天然不含主题关键词，因此"垃圾判定优先于一切"由"无命中即兜底 DEFAULT_TAG"覆盖。
    """
    haystack = (title or "").lower()
    matched: dict[str, list[str]] = {}
    for tag, words in KEYWORDS.items():
        hits = [word for word in words if word.lower() in haystack]
        if hits:
            matched[tag] = hits
    if not matched:
        return None
    if "生活-旅行" in matched and "生活-美食" in matched:
        # 旅行仅在命中住宿/景点/路线这类强证据时才压过美食，否则美食优先。
        if set(matched["生活-旅行"]) & TRAVEL_STRONG_WORDS:
            matched.pop("生活-美食", None)
        else:
            matched.pop("生活-旅行", None)
        if not matched:
            return None
    return min(matched, key=lambda tag: _PRIORITY_RANK.get(tag, len(PRIORITY)))


def _classify_one(tagger: ContentTagger, source: str, title: str) -> tuple[list[str], str, str]:
    """返回 (tags, method, reason)。method ∈ {model, keyword, default}。"""
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = tagger.tag(title=title, source=source)
            cleaned: list[str] = []
            for part in str(raw).split(","):
                tag = normalize_tag(part.strip())
                if tag and tag not in cleaned:
                    cleaned.append(tag)
            if cleaned:
                return cleaned[:2], "model", ""
            last_error = "模型返回空/非法标签"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
    fallback = keyword_fallback(title)
    if fallback:
        return [fallback], "keyword", f"模型 {MAX_ATTEMPTS} 次失败: {last_error}"
    return [DEFAULT_TAG], "default", f"模型 {MAX_ATTEMPTS} 次失败且关键词无法判定: {last_error}"


def build_tagger(env_path: Path = ENV_PATH) -> ContentTagger:
    load_project_env(env_path)
    primary_config = LLMConfig.from_env(env_path)
    try:
        backup_config = LLMConfig.from_env(env_path, backup=True)
    except Exception:  # noqa: BLE001
        backup_config = None
    return ContentTagger(primary_config, backup_config=backup_config)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="laterhub 标签体系 v3 历史数据迁移")
    parser.add_argument("--db", default="", help="目标 SQLite 路径；缺省用 apps.laterhub.config.DB_PATH")
    parser.add_argument("--allow-degraded", action="store_true", help="模型失败率超阈值时仍允许提交")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"路径 B 模型并发度（默认 {DEFAULT_WORKERS}）")
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


def _path_b_rows(rows: list[sqlite3.Row]) -> tuple[list, list, int]:
    """分流：返回 (路径A计划, 路径B待判行, 空标签跳过数)。

    路径A计划为 (link_id, new_tags, old_tags)；路径B行保留原始 Row。
    """
    plan_a: list[tuple[int, str, str]] = []
    path_b: list[sqlite3.Row] = []
    skipped_empty = 0
    for row in rows:
        raw = row["tags"]
        if raw is None or not str(raw).strip():
            skipped_empty += 1
            continue
        parts = split_tags(str(raw))
        if parts and all(part in PATH_A_MAP for part in parts):
            mapped: list[str] = []
            for part in parts:
                new_tag = PATH_A_MAP[part]
                if new_tag not in mapped:
                    mapped.append(new_tag)
            plan_a.append((int(row["id"]), ",".join(mapped[:2]), str(raw)))
        else:
            path_b.append(row)
    return plan_a, path_b, skipped_empty


def migrate(db_path: Path, *, allow_degraded: bool = False, tagger: ContentTagger | None = None, workers: int = DEFAULT_WORKERS) -> int:
    active_tagger = tagger if tagger is not None else build_tagger()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT id, source, title, tags FROM links ORDER BY id ASC").fetchall()
        total_rows = len(rows)
        snapshot = {int(r["id"]): (str(r["source"]), str(r["title"])) for r in rows}
        before_counts: Counter = Counter()
        for row in rows:
            raw = row["tags"]
            if raw is None or not str(raw).strip():
                continue
            for part in split_tags(str(raw)):
                before_counts[part] += 1

        plan_a, path_b, skipped_empty = _path_b_rows(rows)

        # 路径 B：并发只调模型，返回 (id, tags, method, reason)；写库全部回主线程串行。
        results: dict[int, tuple[list[str], str, str]] = {}
        if path_b:
            print(f"[迁移] 路径 B 交模型重判 {len(path_b)} 行，并发度 {workers} ...")
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {
                    pool.submit(_classify_one, active_tagger, str(row["source"]), str(row["title"])): int(row["id"])
                    for row in path_b
                }
                for future, link_id in futures.items():
                    results[link_id] = future.result()

        updates: list[tuple[str, int, str]] = []
        after_counts: Counter = Counter()
        method_counts: Counter = Counter()
        failures: list[str] = []
        path_b_by_id = {int(row["id"]): row for row in path_b}

        for link_id, new_tags, old_tags in plan_a:
            method_counts["rename"] += 1
            after_counts.update(new_tags.split(","))
            updates.append((new_tags, link_id, old_tags))

        for link_id, (tags, method, reason) in results.items():
            method_counts[method] += 1
            after_counts.update(tags)
            raw = str(path_b_by_id[link_id]["tags"])
            updates.append((",".join(tags), link_id, raw))
            if method != "model":
                row = path_b_by_id[link_id]
                failures.append(
                    f"id={link_id} | source={row['source']} | title={str(row['title'])[:60]} | {reason}"
                )

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        changed = 0
        for new_tags, link_id, old_tags in updates:
            if new_tags == old_tags:
                # 标签没变就不动 updated_at：它是 recover_stale_digest_runs 判断"简报任务
                # 卡死"的唯一依据，全量刷新会把中断的任务推迟最多 2 小时才重新排队。
                continue
            conn.execute(
                "UPDATE links SET tags = ?, updated_at = ? WHERE id = ?",
                (new_tags, now, link_id),
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

        path_b_total = len(results)
        model_failures = int(method_counts["keyword"] + method_counts["default"])
        failure_rate = (model_failures / path_b_total) if path_b_total else 0.0

        print("[迁移] 统计：")
        print(f"  总行数: {total_rows}")
        print(f"  空标签跳过: {skipped_empty}")
        print(f"  实际改写行数: {changed}")
        print(f"  路径A 1:1 改名行数: {method_counts['rename']}")
        print(f"  路径B 重判行数: {path_b_total}（成功重判 {method_counts['model']}）")
        print(f"  关键词兜底数: {method_counts['keyword']}")
        print(f"  落 {DEFAULT_TAG} 的兜底数: {method_counts['default']}")
        print(f"  模型失败行数: {model_failures}，失败率: {failure_rate:.1%}")
        print("  迁移后标签分布（按 catalog 顺序）:")
        for tag in TAG_OPTIONS:
            print(f"    {tag}: {after_counts.get(tag, 0)}")
        for tag, count in after_counts.items():
            if tag not in TAG_WHITELIST:
                print(f"    [非法]{tag}: {count}")
        if failures:
            print("  失败明细（行主键 | source | title 前 60 字 | 失败原因）:")
            for line in failures:
                print(f"    {line}")

        if failure_rate > DEGRADED_FAILURE_RATE and not allow_degraded:
            print(
                f"[迁移][警告] 模型失败率 {failure_rate:.1%} 超过 {DEGRADED_FAILURE_RATE:.0%} 阈值："
                "已回滚，未提交。确认可接受降级后请加 --allow-degraded 重跑。"
            )
            conn.rollback()
            return 2

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
    args = parse_args(argv)
    db_path = resolve_db_path(args.db)
    probe = sqlite3.connect(db_path)
    try:
        assert_has_links_table(probe, db_path)
    finally:
        probe.close()
    backup_db(db_path)
    return migrate(db_path, allow_degraded=args.allow_degraded, workers=args.workers)


if __name__ == "__main__":
    raise SystemExit(main())

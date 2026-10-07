"""laterhub 标签体系 v5 最小自检（无框架、无 fixture）。

运行：python scripts/check_laterhub_tag_taxonomy.py [--db <sqlite 路径>]
通过时退出码 0；任一断言失败即抛错退出非 0。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from apps.laterhub.config import DB_PATH as DEFAULT_DB_PATH
from apps.laterhub.pipeline import parse_tags_text
from apps.laterhub.tagger import DEFAULT_TAG, SYSTEM_PROMPT, TAG_OPTIONS, TAG_WHITELIST, normalize_tag

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # noqa: BLE001
    pass

TAG_COUNT = 27

# v4 → v5 起已消失的旧标签（改名或合并）。用"解析出的标签集合"做精确成员判断，而非子串
# 匹配——「心理_认知/学习」是「心理_认知/学习方法」的前缀、「社科_哲学」是
# 「社科_哲学/社会/经济/金融」的前缀、「社科_艺术」是「社科_艺术/音乐」的前缀，子串
# 判断会把新合法标签误报为废弃。
# 注意：v4 的另外 22 个名字（心理_他人/关系、技术_AI、生活_*、娱乐_*、其他_无法判断…）
# 在 v5 仍然合法，本表刻意不收录——收录后"不在白名单"断言必然自相矛盾。
DEPRECATED_TAGS = (
    # v4 中已消失的 8 个名（v5 改名/合并掉）
    "心理_认知/学习",
    "心理_自我/情绪",
    "社科_社会",
    "社科_经济/金融",
    "社科_哲学",
    "社科_艺术",
    "社科_音乐",
    "社科_玄学",
    # v3 连字符名（v4 起全部废弃）
    "心理-认知",
    "心理-自我/情绪",
    "心理-他人/关系",
    "心理-职场",
    "技术-AI",
    "技术-英语",
    "技术-摄影",
    "技术-唱歌",
    "技术-写作",
    "技术-工具",
    "技术-开发",
    "社科-科学/科普",
    "社科-历史",
    "社科-社会",
    "社科-经济/金融",
    "社科-哲学",
    "社科-艺术",
    "社科-音乐",
    "社科-玄学",
    "生活-健康",
    "生活-健身",
    "生活-穿搭",
    "生活-保养",
    "生活-美食",
    "生活-旅行",
    "娱乐-影视/游戏/动漫/解说",
    "娱乐-文学/解说",
    "娱乐-综艺",
    "娱乐-吃瓜",
    "其他-无法判断",
)
APPROX_UNKNOWN = DEPRECATED_TAGS + (
    "人文-哲学",
    "人文-玄学",
    "解说-文学",
    "解说-影视/游戏/动漫",
    "技术-创作/表达",
    "技术-效率/工具/开发",
    "社科-金融/商业/消费",
    "文化-哲学/文学/艺术/音乐",
    "生活-健康/健身",
    # 合法 v5 名的前缀 / 已废弃名的近似串：全部必须返回 None，不得被吞进某个合法标签
    "心理_认知",
    "心理_认知/学习/",
    "心理_自我/情绪/",
    "社科_艺术/",
    "社科_音乐/",
)


def parse_prompt_tags(prompt: str) -> list[str]:
    """从提示词【唯一标签清单】段落解析标签名（不含定义行）。"""
    marker = "合并标签：\n"
    start = prompt.index(marker) + len(marker)
    end = prompt.index("\n\n【标签定义】", start)
    return [line.strip() for line in prompt[start:end].split("\n") if line.strip()]


def check_prompt_whitelist_consistency() -> None:
    prompt_tags = parse_prompt_tags(SYSTEM_PROMPT)
    assert set(prompt_tags) == set(TAG_OPTIONS), (
        f"提示词标签与 TAG_OPTIONS 不一致: 仅提示词={set(prompt_tags) - set(TAG_OPTIONS)}, "
        f"仅白名单={set(TAG_OPTIONS) - set(prompt_tags)}"
    )
    assert len(prompt_tags) == len(TAG_OPTIONS) == TAG_COUNT, (
        f"标签数量异常: prompt={len(prompt_tags)} options={len(TAG_OPTIONS)} 期望={TAG_COUNT}"
    )
    assert prompt_tags == TAG_OPTIONS, "提示词标签顺序应与 TAG_OPTIONS 一致"


def check_no_deprecated() -> None:
    prompt_tags = set(parse_prompt_tags(SYSTEM_PROMPT))
    for tag in DEPRECATED_TAGS:
        assert tag not in prompt_tags, f"提示词残留已废弃标签: {tag}"
        assert tag not in TAG_WHITELIST, f"白名单残留已废弃标签: {tag}"
    print(f"[自检] 提示词与白名单均无 {len(DEPRECATED_TAGS)} 个已废弃标签")


def check_normalize() -> None:
    assert DEFAULT_TAG in TAG_WHITELIST, f"DEFAULT_TAG 不在白名单: {DEFAULT_TAG}"
    assert len(TAG_OPTIONS) == TAG_COUNT
    for tag in TAG_OPTIONS:
        assert normalize_tag(tag) == tag, f"合法标签未原样归一化: {tag}"
    for bad in APPROX_UNKNOWN:
        assert normalize_tag(bad) is None, f"近似/废弃串不应命中白名单: {bad}"
    for bad in DEPRECATED_TAGS:
        assert normalize_tag(bad) is None, f"已废弃标签不应命中白名单: {bad}"
    assert parse_tags_text("未知标签A,未知标签B") == [DEFAULT_TAG], "全未知输入应兜底为 DEFAULT_TAG"
    assert parse_tags_text("解说-文学") == [DEFAULT_TAG]
    assert parse_tags_text("娱乐-影视/游戏/动漫") == [DEFAULT_TAG]
    assert parse_tags_text(None) == [DEFAULT_TAG]
    assert parse_tags_text("技术_AI,技术_AI") == ["技术_AI"]
    # 尾空格属于可安全裁剪的噪声：normalize_tag 裁剪后应命中合法标签（不是近似匹配）。
    assert normalize_tag("技术_AI ") == "技术_AI", "尾空格应裁剪后命中合法标签"
    print(f"[自检] {TAG_COUNT} 个合法标签原样通过；近似/废弃串返回 None；未知输入兜底为 {DEFAULT_TAG}")


def check_db(db_arg: str) -> bool:
    """校验库内标签合法；返回是否真的执行了 DB 校验。"""
    db_path = Path(db_arg) if db_arg else Path(DEFAULT_DB_PATH)
    if not db_path.exists():
        # --db 是显式指定的：库不在就是失败，不能被"跳过"掩盖成通过。
        assert not db_arg, f"指定的数据库不存在: {db_path}"
        print(f"[自检] 默认数据库不存在，未执行 DB 校验: {db_path}")
        return False
    conn = sqlite3.connect(db_path)
    try:
        has_links = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='links'"
        ).fetchone()
        if not has_links:
            print(f"[自检] 数据库无 links 表，未执行 DB 校验: {db_path}")
            return False
        bad: set[str] = set()
        checked = 0
        for row in conn.execute("SELECT tags FROM links WHERE COALESCE(TRIM(tags), '') <> ''").fetchall():
            for tag in str(row[0]).replace("，", ",").replace("、", ",").split(","):
                tag = tag.strip()
                if not tag:
                    continue
                checked += 1
                if tag not in TAG_WHITELIST:
                    bad.add(tag)
        assert not bad, f"数据库存在非白名单标签: {sorted(bad)}"
        print(f"[自检] DB 校验通过: {db_path}（检查了 {checked} 个非空标签项，全部在白名单内）")
        return True
    finally:
        conn.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="laterhub 标签体系 v5 自检")
    parser.add_argument("--db", default="", help="可选：校验指定 SQLite；缺省用 apps.laterhub.config.DB_PATH")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    check_prompt_whitelist_consistency()
    print(f"[自检] 提示词标签集合 == TAG_OPTIONS（{len(TAG_OPTIONS)} 个），双向无差集")
    check_no_deprecated()
    check_normalize()
    db_checked = check_db(args.db)
    print(f"[自检] 全部通过（DB 校验: {'已执行' if db_checked else '未执行'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

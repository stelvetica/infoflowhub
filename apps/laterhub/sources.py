"""laterhub 内置抓取源的唯一元数据注册表（SSOT）。

源名称/渠道/抓取方式/依赖认证等只在这里定义；配置文件只持久化 enabled，
避免两处定义漂移。`manual_verify` 是人工补录入口，不是抓取源，不在此表内。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.laterhub.config import PROJECT_ROOT
from infra.utf8_json import dump_json_utf8, load_json_utf8

SOURCES_PATH = PROJECT_ROOT / "config" / "laterhub_sources.json"

# 统一抓取策略（SSOT）：日常短抓取（complete 恒 False，不参与移除判定），
# 每 15 天一次全量（complete 由各源权威信号决定），只有「全量且完整」才允许自动完成。
DEEP_SCAN_INTERVAL_DAYS = 15
# 收藏对照的入库时间下界：早于此刻入库的条目多来自外部迁移，本就不在账号里，
# 拿账号列表去比只会整批误标。四源共用同一时刻。
COMPARE_AFTER = "2026-03-24T08:48:36+00:00"


@dataclass(frozen=True, slots=True)
class LaterhubSource:
    id: str
    name: str
    purpose: str
    channel: str
    fetch_mode: str
    auth_key: str
    flag: str            # argparse 属性名
    fetcher_name: str    # pipeline 模块内的函数名（保留单测 monkeypatch 打桩点）
    needs_browser: bool
    # 源能拉全量、且「消失即已处理」的源才置 True：本轮抓全后，源里不再出现的未完成条目自动完成。
    finish_on_removed: bool
    # 非 None 表示该源支持「深度全量扫描」并按此天数到期：日常只抓增量，到期那轮才滚到底。
    deep_scan_interval_days: int | None = None
    # ISO 时间下界：早于此刻入库的条目不参与「源内已移除」对照。入库早于此刻的多是外部
    # 迁移/导入内容，本就不在账号收藏里，拿账号列表去比只会整批误标。None = 全部参与。
    compare_after: str | None = None


SOURCES: tuple[LaterhubSource, ...] = (
    LaterhubSource(
        "bilibili_watchlater", "B站稍后看", "收藏/稍后处理", "Bilibili", "网页抓取（bsk）", "",
        "fetch_bilibili", "fetch_bilibili_watchlater", True,
        finish_on_removed=True,
        deep_scan_interval_days=DEEP_SCAN_INTERVAL_DAYS,
        compare_after=COMPARE_AFTER,
    ),
    LaterhubSource(
        "douyin_favorite", "抖音收藏", "收藏/稍后处理", "抖音", "网页抓取（bsk）", "douyin_shared",
        "fetch_douyin", "fetch_douyin_favorites", True,
        finish_on_removed=True,
        deep_scan_interval_days=DEEP_SCAN_INTERVAL_DAYS,
        # 以账号内最早一条收藏（aweme 7547973316054977835）的收录时刻为下界。
        compare_after=COMPARE_AFTER,
    ),
    LaterhubSource(
        "xiaoheihe_favor", "小黑盒收藏", "收藏/稍后处理", "小黑盒", "网页抓取（bsk）", "xiaoheihe_shared",
        "fetch_xiaoheihe", "fetch_xiaoheihe_favorites", True,
        finish_on_removed=True,
        deep_scan_interval_days=DEEP_SCAN_INTERVAL_DAYS,
        compare_after=COMPARE_AFTER,
    ),
    LaterhubSource(
        "weread_shelf", "微信读书书架", "待读/稍后处理", "微信读书", "网页抓取（bsk）", "",
        "fetch_weread", "fetch_weread_shelf", True,
        # 书架移除 = 读完/放弃，深扫完整时自动完成；keep_finished 仍保留：
        # 重新加回书架不会自动复活（否则每次抓取都会把用户手动完成的书翻回待办）。
        finish_on_removed=True,
        deep_scan_interval_days=DEEP_SCAN_INTERVAL_DAYS,
        compare_after=COMPARE_AFTER,
    ),
)
SOURCE_BY_ID = {source.id: source for source in SOURCES}


def load_enabled_map() -> dict[str, bool]:
    """读取各源启停状态；文件缺失/结构非法/单条非法一律按默认 True（fail-open）。

    配置写坏时宁可多抓也不要让全部源停摆；未知 id 直接忽略。
    """
    enabled = {source.id: True for source in SOURCES}
    try:
        payload: Any = load_json_utf8(SOURCES_PATH)
    except Exception:
        return enabled
    rows = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return enabled
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_id = row.get("id")
        value = row.get("enabled")
        if source_id in SOURCE_BY_ID and isinstance(value, bool):
            enabled[source_id] = value
    return enabled


def save_enabled_map(mapping: dict[str, bool]) -> None:
    """只写注册表内的 id，按 SOURCES 顺序落盘。"""
    rows = [
        {"id": source.id, "enabled": bool(mapping.get(source.id, True))}
        for source in SOURCES
    ]
    dump_json_utf8(SOURCES_PATH, {"sources": rows})


def is_enabled(source_id: str) -> bool:
    return load_enabled_map().get(source_id, True)


def set_enabled(source_id: str, enabled: bool) -> None:
    if source_id not in SOURCE_BY_ID:
        raise KeyError(f"未注册的稍后处理源: {source_id}")
    mapping = load_enabled_map()
    mapping[source_id] = bool(enabled)
    save_enabled_map(mapping)

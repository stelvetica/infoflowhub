from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from typing import Any

from apps.laterhub.config import DB_PATH, ENV_PATH, LOG_PATH, LOGS_DIR
from apps.laterhub.db import DBManager, LinkRecord
from apps.laterhub.feishu import FeishuBitableClient, FeishuConfig, load_project_env
from apps.laterhub.tagger import ContentTagger, DEFAULT_TAG, LLMConfig
from connectors.bilibili import fetch_bilibili_watchlater
from connectors.douyin import fetch_douyin_favorites


LOG_FILE_DISABLED = False

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass


def _iso_to_millis(iso_text: str) -> int:
    return int(datetime.fromisoformat(iso_text).timestamp() * 1000)


def parse_tags_text(tags: str | None) -> list[str]:
    text = (tags or "").strip()
    if not text:
        return [DEFAULT_TAG]
    normalized = re.sub(r"[，、]+", ",", text.replace("/ ", "/"))
    parts = [part.strip() for part in normalized.split(",") if part.strip()]
    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return deduped or [DEFAULT_TAG]


def build_feishu_fields(row: Any) -> dict[str, Any]:
    return {
        "标题": row["title"],
        "标签": parse_tags_text(row["tags"]),
        "链接": {"text": row["title"], "link": row["url"]},
        "已看完": False,
        "来源": row["source"],
        "入库时间": _iso_to_millis(row["created_at"]),
        "推送状态": row["status"],
    }


def log_line(message: str) -> None:
    global LOG_FILE_DISABLED
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamped = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
    print(message)
    if LOG_FILE_DISABLED:
        return
    try:
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(stamped + "\n")
    except OSError as exc:
        LOG_FILE_DISABLED = True
        print(f"[警告] 日志文件不可写，已跳过文件日志 {LOG_PATH} -> {exc}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PKM Auto-Hub 主流程")
    parser.add_argument("--retry-failed", action="store_true", help="运行前把 failed 记录重置为 pending 再重推")
    parser.add_argument("--fetch-bilibili", action="store_true", help="抓取 B 站稍后看并写入本地数据库")
    parser.add_argument("--fetch-douyin", action="store_true", help="抓取抖音收藏并写入本地数据库")
    return parser.parse_args(argv)


def _load_tagger() -> ContentTagger | None:
    try:
        primary_config = LLMConfig.from_env(ENV_PATH)
        try:
            backup_config = LLMConfig.from_env(ENV_PATH, backup=True)
        except Exception:
            backup_config = None
        log_line("[补充] 已加载主/备标签模型配置")
        return ContentTagger(primary_config, backup_config=backup_config)
    except Exception as exc:  # noqa: BLE001
        log_line(f"[补充] 标签模型不可用，先回退默认标签: {exc}")
        return None


def _save_items(db: DBManager, items: list[dict[str, Any]]) -> None:
    for item in items:
        db.upsert_link(LinkRecord(url=item["url"], title=item["title"], source=item["source"], tags=item.get("tags")))


def _fetch_source(*, enabled: bool, label: str, fetcher, db: DBManager) -> bool:
    if not enabled:
        return False
    log_line(f"[3/6] 抓取{label}")
    try:
        fetched = fetcher(ENV_PATH)
        _save_items(db, fetched)
        log_line(f"[3/6] {label}抓取完成，共 {len(fetched)} 条")
    except Exception as exc:  # noqa: BLE001
        log_line(f"[3/6] {label}抓取失败，但不中断主流程: {exc}")
    return True


def _tag_pending_rows(db: DBManager, tagger: ContentTagger | None) -> None:
    log_line("[4/6] 为待推送记录补齐标签")
    rows_to_prepare = db.list_by_status("pending")
    for row in rows_to_prepare:
        tags = row["tags"] or DEFAULT_TAG
        if tagger:
            try:
                tags = tagger.tag(title=row["title"], source=row["source"])
            except Exception as exc:  # noqa: BLE001
                log_line(f"  - 标签生成失败，回退默认标签: {row['title']} -> {exc}")
                tags = row["tags"] or DEFAULT_TAG
        db.update_tags(row["url"], tags)


def _push_pending_rows(db: DBManager) -> int:
    rows_to_push = db.list_by_status("pending")
    if not rows_to_push:
        log_line("[5/6] 没有需要推送到飞书的新数据，流程结束")
        return 0
    log_line(f"[5/6] 检查飞书配置 {ENV_PATH}")
    client = FeishuBitableClient(FeishuConfig.from_env(ENV_PATH))
    log_line(f"[6/6] 准备推送 {len(rows_to_push)} 条记录到飞书")
    success_count = 0
    for row in rows_to_push:
        try:
            response = client.create_record(build_feishu_fields(row))
            record_id = ((response.get("data") or {}).get("record") or {}).get("record_id")
            db.mark_pushed(row["url"], feishu_record_id=record_id)
            success_count += 1
            log_line(f"  - 已推送 {row['title']}")
        except Exception as exc:  # noqa: BLE001
            db.mark_failed(row["url"], str(exc))
            log_line(f"  - 推送失败 {row['title']} -> {exc}")
    log_line(f"完成: 成功 {success_count} 条，失败 {len(rows_to_push) - success_count} 条")
    return 0


def run(args: argparse.Namespace) -> int:
    load_project_env(ENV_PATH)
    log_line("[1/6] 初始化数据库")
    db = DBManager(DB_PATH)
    tagger = _load_tagger()
    if args.retry_failed:
        reset_count = db.reset_failed_to_pending()
        log_line(f"[补充] 已重置 failed 记录 {reset_count} 条")
    log_line("[2/6] 准备抓取与整理流程")
    fetched_any = False
    fetched_any = _fetch_source(enabled=args.fetch_bilibili, label="B 站稍后看", fetcher=fetch_bilibili_watchlater, db=db) or fetched_any
    fetched_any = _fetch_source(enabled=args.fetch_douyin, label="抖音收藏", fetcher=fetch_douyin_favorites, db=db) or fetched_any
    if not fetched_any:
        log_line("[3/6] 跳过外部抓取")
    _tag_pending_rows(db, tagger)
    return _push_pending_rows(db)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))

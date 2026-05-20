from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from apps.laterhub.config import DB_PATH, ENV_PATH, LOG_PATH, LOGS_DIR
from apps.laterhub.db import DBManager, LinkRecord
from apps.laterhub.feishu import FeishuBitableClient, FeishuConfig, load_project_env
from apps.laterhub.tagger import (
    ContentTagger,
    DEFAULT_TAG,
    LLMConfig,
    TaggerPermanentError,
    TaggerServiceUnavailable,
    TaggerTemporaryError,
)
from connectors.bilibili import fetch_bilibili_watchlater
from connectors.douyin import fetch_douyin_favorites


LOG_FILE_DISABLED = False
ENABLE_FEISHU_PUSH = False
TAG_RETRY_LIMIT = 2

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
    normalized = (
        text.replace("/ ", "/")
        .replace("：", ",")
        .replace(":", ",")
        .replace("?", ",")
        .replace(";", ",")
        .replace("|", ",")
    )
    normalized = re.sub(r"[？、]+", ",", normalized)
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
        "已读": False,
        "来源": row["source"],
        "创建时间": _iso_to_millis(row["created_at"]),
        "状态": row["status"],
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
        print(f"[日志] 写入失败 {LOG_PATH} -> {exc}")


def _brief_title(row: Any) -> str:
    title = str(row["title"]).strip()
    return title if len(title) <= 48 else f"{title[:45]}..."


def _is_temporary_upstream_error(exc: Exception) -> bool:
    if isinstance(exc, requests.RequestException):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in {429, 502, 503, 504}:
            return True
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
    message = str(exc).lower()
    temporary_markers = (
        "http 429",
        "http 502",
        "http 503",
        "http 504",
        "timeout",
        "timed out",
        "connection reset",
        "temporarily unavailable",
    )
    return any(marker in message for marker in temporary_markers)


def _format_temporary_log(stage: str, row: Any, reason: str) -> str:
    return f"[{stage}][临时波动] {_brief_title(row)} | {row['source']} | {reason}"


def _format_failure_log(stage: str, row: Any, reason: str) -> str:
    return f"[{stage}][真实失败] {_brief_title(row)} | {row['source']} | {reason}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PKM Auto-Hub laterhub 流水线")
    parser.add_argument("--retry-failed", action="store_true", help="将 failed 条目重置回 pending 后重跑")
    parser.add_argument("--fetch-bilibili", action="store_true", help="抓取 B 站稍后看")
    parser.add_argument("--fetch-douyin", action="store_true", help="抓取抖音收藏")
    return parser.parse_args(argv)


def _load_tagger() -> ContentTagger | None:
    try:
        primary_config = LLMConfig.from_env(ENV_PATH)
        try:
            backup_config = LLMConfig.from_env(ENV_PATH, backup=True)
        except Exception:
            backup_config = None
        if backup_config:
            log_line("[标签] 已加载主/备 LLM 配置")
        else:
            log_line("[标签] 已加载主 LLM，当前未配置备用 LLM")
        return ContentTagger(primary_config, backup_config=backup_config)
    except Exception as exc:  # noqa: BLE001
        log_line(f"[标签] LLM 配置不可用，本轮仅保留无标签待下次重试: {exc}")
        return None


def load_tagger() -> ContentTagger | None:
    load_project_env(ENV_PATH)
    return _load_tagger()


@dataclass(slots=True)
class LaterhubRunSummary:
    fetched_sources: int = 0
    pending_total: int = 0
    push_enabled: bool = ENABLE_FEISHU_PUSH


def _save_items(db: DBManager, items: list[dict[str, Any]]) -> None:
    for item in items:
        db.upsert_link(LinkRecord(url=item["url"], title=item["title"], source=item["source"], tags=item.get("tags")))


def _fetch_source(*, enabled: bool, label: str, fetcher, db: DBManager) -> bool:
    if not enabled:
        return False
    log_line(f"[3/6] 开始抓取{label}")
    try:
        fetched = fetcher(ENV_PATH)
        _save_items(db, fetched)
        log_line(f"[3/6] {label}抓取完成，新增/更新 {len(fetched)} 条")
    except Exception as exc:  # noqa: BLE001
        log_line(f"[3/6] {label}抓取失败: {exc}")
    return True


def _tag_single_row(db: DBManager, row: Any, tagger: ContentTagger) -> str:
    tags = tagger.tag(title=row["title"], source=row["source"])
    db.update_tags(row["url"], tags)
    return tags


def _retry_temporary_tag_rows(db: DBManager, tagger: ContentTagger, rows: list[Any]) -> tuple[int, int]:
    retried_success = 0
    still_pending = 0
    for row in rows:
        success = False
        last_reason = ""
        for attempt in range(1, TAG_RETRY_LIMIT + 1):
            try:
                _tag_single_row(db, row, tagger)
                retried_success += 1
                log_line(f"[标签][补偿成功] {_brief_title(row)} | 第 {attempt} 次补偿成功")
                success = True
                break
            except (TaggerTemporaryError, TaggerServiceUnavailable) as exc:
                last_reason = str(exc)
                log_line(_format_temporary_log("标签补偿", row, f"第 {attempt} 次补偿仍失败: {exc}"))
            except TaggerPermanentError as exc:
                last_reason = str(exc)
                db.mark_tag_pending_retry(row["url"], f"标签真实失败，留空待后续人工/统一重试: {exc}")
                log_line(_format_failure_log("标签补偿", row, f"停止补偿，留空待后续处理: {exc}"))
                success = True
                break
            except Exception as exc:  # noqa: BLE001
                last_reason = str(exc)
                db.mark_tag_pending_retry(row["url"], f"标签未知失败，留空待后续统一重试: {exc}")
                log_line(_format_failure_log("标签补偿", row, f"停止补偿，留空待后续处理: {exc}"))
                success = True
                break
        if success:
            continue
        still_pending += 1
        db.mark_tag_pending_retry(row["url"], f"LLM 临时波动，补偿 {TAG_RETRY_LIMIT} 次后仍失败，留空待下轮统一补标签: {last_reason}")
        log_line(_format_temporary_log("标签补偿", row, f"补偿 {TAG_RETRY_LIMIT} 次仍失败，留空待下轮统一补标签"))
    return retried_success, still_pending


def _tag_pending_rows(db: DBManager, tagger: ContentTagger | None) -> None:
    log_line("[4/6] 开始准备标签")
    rows_to_prepare = db.list_untagged_rows()
    if not rows_to_prepare:
        log_line("[4/6] 没有需要补标签的无标签条目")
        return
    if not tagger:
        log_line(f"[4/6] 当前无可用 LLM，{len(rows_to_prepare)} 条继续留空，等待下轮统一补标签")
        return

    prepared_count = 0
    permanent_empty_count = 0
    temporary_rows: list[Any] = []
    for row in rows_to_prepare:
        try:
            _tag_single_row(db, row, tagger)
            prepared_count += 1
        except (TaggerTemporaryError, TaggerServiceUnavailable) as exc:
            temporary_rows.append(row)
            db.mark_tag_pending_retry(row["url"], f"LLM 临时波动，先跳过本轮首轮打标: {exc}")
            log_line(_format_temporary_log("标签", row, f"先跳过，待本轮尾部统一补偿: {exc}"))
        except TaggerPermanentError as exc:
            permanent_empty_count += 1
            db.mark_tag_pending_retry(row["url"], f"标签真实失败，留空待后续统一补标签: {exc}")
            log_line(_format_failure_log("标签", row, f"留空，待后续统一补标签: {exc}"))
        except Exception as exc:  # noqa: BLE001
            permanent_empty_count += 1
            db.mark_tag_pending_retry(row["url"], f"标签未知失败，留空待后续统一补标签: {exc}")
            log_line(_format_failure_log("标签", row, f"留空，待后续统一补标签: {exc}"))

    retried_success = 0
    still_pending = 0
    if temporary_rows:
        log_line(f"[4/6] 首轮临时波动 {len(temporary_rows)} 条，开始尾部统一补偿 {TAG_RETRY_LIMIT} 次")
        retried_success, still_pending = _retry_temporary_tag_rows(db, tagger, temporary_rows)

    log_line(
        f"[4/6] 标签完成，首轮成功 {prepared_count} 条，补偿成功 {retried_success} 条，"
        f"留空待下轮 {still_pending + permanent_empty_count} 条"
    )


def prepare_pending_tags(db: DBManager | None = None, tagger: ContentTagger | None = None) -> int:
    active_db = db or DBManager(DB_PATH)
    active_tagger = tagger if tagger is not None else load_tagger()
    _tag_pending_rows(active_db, active_tagger)
    return len(active_db.list_by_status("pending"))


def _push_pending_rows(db: DBManager) -> int:
    if not ENABLE_FEISHU_PUSH:
        pending_count = len(db.list_by_status("pending"))
        log_line(f"[5/6] 飞书推送未开启，当前仍有 {pending_count} 条 pending")
        return 0
    rows_to_push = db.list_by_status("pending")
    if not rows_to_push:
        log_line("[5/6] 没有待推送条目")
        return 0
    log_line(f"[5/6] 加载飞书配置 {ENV_PATH}")
    client = FeishuBitableClient(FeishuConfig.from_env(ENV_PATH))
    log_line(f"[6/6] 开始推送 {len(rows_to_push)} 条记录")
    success_count = 0
    retry_count = 0
    failed_count = 0
    for row in rows_to_push:
        try:
            response = client.create_record(build_feishu_fields(row))
            record_id = ((response.get("data") or {}).get("record") or {}).get("record_id")
            db.mark_pushed(row["url"], feishu_record_id=record_id)
            success_count += 1
            log_line(f"  - 推送成功 {row['title']}")
        except Exception as exc:  # noqa: BLE001
            if _is_temporary_upstream_error(exc):
                db.mark_pending_retry(row["url"], f"临时服务波动，等待重试: {exc}")
                retry_count += 1
                log_line(_format_temporary_log("推送", row, f"保留 pending，等待下轮自动重试: {exc}"))
                continue
            db.mark_failed(row["url"], str(exc))
            failed_count += 1
            log_line(_format_failure_log("推送", row, str(exc)))
    log_line(f"[6/6] 推送完成，成功 {success_count} 条，待重试 {retry_count} 条，真实失败 {failed_count} 条")
    return 0


def run(args: argparse.Namespace) -> int:
    load_project_env(ENV_PATH)
    log_line("[1/6] 初始化 laterhub")
    db = DBManager(DB_PATH)
    tagger = _load_tagger()
    if args.retry_failed:
        reset_count = db.reset_failed_to_pending()
        log_line(f"[补偿] 已将 failed 重置回 pending {reset_count} 条")
    log_line("[2/6] 开始抓取稍后读来源")
    fetched_any = False
    fetched_any = _fetch_source(enabled=args.fetch_bilibili, label="B 站稍后看", fetcher=fetch_bilibili_watchlater, db=db) or fetched_any
    fetched_any = _fetch_source(enabled=args.fetch_douyin, label="抖音收藏", fetcher=fetch_douyin_favorites, db=db) or fetched_any
    if not fetched_any:
        log_line("[3/6] 本轮未启用任何抓取来源")
    _tag_pending_rows(db, tagger)
    return _push_pending_rows(db)


def run_main_flow(*, fetch_bilibili: bool = True, fetch_douyin: bool = True, retry_failed: bool = False) -> LaterhubRunSummary:
    args = argparse.Namespace(
        retry_failed=retry_failed,
        fetch_bilibili=fetch_bilibili,
        fetch_douyin=fetch_douyin,
    )
    run(args)
    db = DBManager(DB_PATH)
    fetched_sources = int(bool(fetch_bilibili)) + int(bool(fetch_douyin))
    pending_total = len(db.list_by_status("pending"))
    return LaterhubRunSummary(
        fetched_sources=fetched_sources,
        pending_total=pending_total,
        push_enabled=ENABLE_FEISHU_PUSH,
    )


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))

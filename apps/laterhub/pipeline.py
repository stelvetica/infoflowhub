from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from apps.laterhub.config import DB_PATH, ENV_PATH, LOG_PATH, LOGS_DIR
from apps.laterhub.db import DBManager, LinkRecord
from apps.laterhub.feishu import FeishuBitableClient, FeishuConfig, load_project_env
from apps.laterhub.tagger import ContentTagger, DEFAULT_TAG, LLMConfig, TaggerServiceUnavailable
from connectors.bilibili import fetch_bilibili_watchlater
from connectors.douyin import fetch_douyin_favorites


LOG_FILE_DISABLED = False
ENABLE_FEISHU_PUSH = False

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
    normalized = re.sub(r"[??]+", ",", normalized)
    parts = [part.strip() for part in normalized.split(",") if part.strip()]
    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return deduped or [DEFAULT_TAG]


def build_feishu_fields(row: Any) -> dict[str, Any]:
    return {
        "??": row["title"],
        "??": parse_tags_text(row["tags"]),
        "??": {"text": row["title"], "link": row["url"]},
        "???": False,
        "??": row["source"],
        "????": _iso_to_millis(row["created_at"]),
        "????": row["status"],
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
        print(f"[??] ??????????????? {LOG_PATH} -> {exc}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PKM Auto-Hub ???")
    parser.add_argument("--retry-failed", action="store_true", help="???? failed ????? pending ???")
    parser.add_argument("--fetch-bilibili", action="store_true", help="?? B ????????????")
    parser.add_argument("--fetch-douyin", action="store_true", help="??????????????")
    return parser.parse_args(argv)


def _load_tagger() -> ContentTagger | None:
    try:
        primary_config = LLMConfig.from_env(ENV_PATH)
        try:
            backup_config = LLMConfig.from_env(ENV_PATH, backup=True)
        except Exception:
            backup_config = None
        log_line("[??] ????/???????")
        return ContentTagger(primary_config, backup_config=backup_config)
    except Exception as exc:  # noqa: BLE001
        log_line(f"[??] ???????????????: {exc}")
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
    log_line(f"[3/6] ??{label}")
    try:
        fetched = fetcher(ENV_PATH)
        _save_items(db, fetched)
        log_line(f"[3/6] {label}?????? {len(fetched)} ?")
    except Exception as exc:  # noqa: BLE001
        log_line(f"[3/6] {label}????????????: {exc}")
    return True


def _tag_pending_rows(db: DBManager, tagger: ContentTagger | None) -> None:
    log_line("[4/6] ??????????")
    rows_to_prepare = db.list_pending_tag_rows()
    if not rows_to_prepare:
        log_line("[4/6] ????????? pending ??")
        return
    if not tagger:
        for row in rows_to_prepare:
            db.mark_tag_skipped(row["url"], row["tags"] or DEFAULT_TAG, "LLM ???????????")
        log_line(f"[4/6] LLM ?????? {len(rows_to_prepare)} ????????????????")
        return

    prepared_count = 0
    skipped_count = 0
    for index, row in enumerate(rows_to_prepare):
        fallback_tags = row["tags"] or DEFAULT_TAG
        try:
            tags = tagger.tag(title=row["title"], source=row["source"])
            db.update_tags(row["url"], tags)
            prepared_count += 1
        except TaggerServiceUnavailable as exc:
            remaining_rows = rows_to_prepare[index:]
            for pending_row in remaining_rows:
                db.mark_tag_skipped(pending_row["url"], pending_row["tags"] or DEFAULT_TAG, str(exc))
            skipped_count += len(remaining_rows)
            log_line(f"[4/6] LLM ??????????? {len(remaining_rows)} ????????: {exc}")
            break
        except Exception as exc:  # noqa: BLE001
            db.mark_tag_skipped(row["url"], fallback_tags, f"????????????: {exc}")
            skipped_count += 1
            log_line(f"  - ?????????????: {row['title']} -> {exc}")

    log_line(f"[4/6] ????????? {prepared_count} ???? {skipped_count} ?")


def prepare_pending_tags(db: DBManager | None = None, tagger: ContentTagger | None = None) -> int:
    active_db = db or DBManager(DB_PATH)
    active_tagger = tagger if tagger is not None else load_tagger()
    _tag_pending_rows(active_db, active_tagger)
    return len(active_db.list_by_status("pending"))


def _push_pending_rows(db: DBManager) -> int:
    if not ENABLE_FEISHU_PUSH:
        pending_count = len(db.list_by_status("pending"))
        log_line(f"[5/6] ??????????? {pending_count} ? pending ??")
        return 0
    rows_to_push = db.list_by_status("pending")
    if not rows_to_push:
        log_line("[5/6] ??????????????????")
        return 0
    log_line(f"[5/6] ?????? {ENV_PATH}")
    client = FeishuBitableClient(FeishuConfig.from_env(ENV_PATH))
    log_line(f"[6/6] ???? {len(rows_to_push)} ??????")
    success_count = 0
    for row in rows_to_push:
        try:
            response = client.create_record(build_feishu_fields(row))
            record_id = ((response.get("data") or {}).get("record") or {}).get("record_id")
            db.mark_pushed(row["url"], feishu_record_id=record_id)
            success_count += 1
            log_line(f"  - ??? {row['title']}")
        except Exception as exc:  # noqa: BLE001
            db.mark_failed(row["url"], str(exc))
            log_line(f"  - ???? {row['title']} -> {exc}")
    log_line(f"??: ?? {success_count} ???? {len(rows_to_push) - success_count} ?")
    return 0


def run(args: argparse.Namespace) -> int:
    load_project_env(ENV_PATH)
    log_line("[1/6] ??????")
    db = DBManager(DB_PATH)
    tagger = _load_tagger()
    if args.retry_failed:
        reset_count = db.reset_failed_to_pending()
        log_line(f"[??] ??? failed ?? {reset_count} ?")
    log_line("[2/6] ?????????")
    fetched_any = False
    fetched_any = _fetch_source(enabled=args.fetch_bilibili, label="B ????", fetcher=fetch_bilibili_watchlater, db=db) or fetched_any
    fetched_any = _fetch_source(enabled=args.fetch_douyin, label="????", fetcher=fetch_douyin_favorites, db=db) or fetched_any
    if not fetched_any:
        log_line("[3/6] ??????")
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

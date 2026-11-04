from __future__ import annotations

import argparse
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from apps.laterhub import sources as source_registry
from apps.laterhub.config import DB_PATH, ENV_PATH, LOG_PATH, LOGS_DIR
from apps.laterhub.db import DBManager, LinkRecord
from apps.laterhub.feishu import FeishuBitableClient, FeishuConfig, load_project_env
from apps.laterhub.health import record_result
from apps.laterhub.tagger import (
    ContentTagger,
    DEFAULT_TAG,
    LLMConfig,
    TaggerPermanentError,
    TaggerServiceUnavailable,
    TaggerTemporaryError,
    normalize_tag,
)
from connectors._shared.bsk_session import BskRunnerSession
from connectors._shared.common import FetchBatch
from connectors.bilibili import fetch_bilibili_watchlater
from connectors.douyin import fetch_douyin_favorites
from connectors.weread import fetch_weread_shelf
from connectors.xiaoheihe import fetch_xiaoheihe_favorites


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
        # 逐项严格白名单归一化：未知标签直接丢弃，只留合法项，最多 2 个
        tag = normalize_tag(part)
        if tag and tag not in deduped:
            deduped.append(tag)
    return deduped[:2] or [DEFAULT_TAG]


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
    parser.add_argument("--fetch-xiaoheihe", action="store_true", help="抓取小黑盒收藏")
    parser.add_argument("--fetch-weread", action="store_true", help="抓取微信读书书架")
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


# 各源抓取超时（秒）。bsk 复用用户日常 Chrome，页面/CDP 假死时浏览器端超时可能失效，
# 超过此上限不再等待该源：停会话并隔离它，后续源各自新建会话继续，避免卡死整轮。
SOURCE_FETCH_TIMEOUT_SECONDS = 180


class _SourceFetchTimeout(RuntimeError):
    """单源抓取超时：该源会话状态已不可信，调用方须置空会话并隔离该源。"""


def _save_items(db: DBManager, items: list[dict[str, Any]]) -> None:
    for item in items:
        db.upsert_link(
            LinkRecord(
                url=item["url"],
                title=item["title"],
                source=item["source"],
                tags=item.get("tags"),
                keep_finished=bool(item.get("keep_finished")),
            )
        )


def _run_fetcher_with_timeout(
    label: str, fetcher, session=None, *, owns_session: bool = False
) -> list[dict[str, Any]]:
    """守护线程执行 fetcher，超时则停会话并抛 _SourceFetchTimeout。

    daemon 线程即使卡死也不阻塞进程退出。ponytail: 守护线程无法强杀卡死的 Python worker，
    只能停 bsk 会话并把该源隔离；卡死的 worker 随进程退出终止，后续源各自新建会话继续。
    """
    from queue import Empty, Queue

    result_queue: "Queue[tuple[str, object]]" = Queue(maxsize=1)

    def _worker() -> None:
        try:
            fetched = fetcher(ENV_PATH, session=session) if session is not None else fetcher(ENV_PATH)
            result_queue.put(("ok", fetched))
        except BaseException as exc:  # noqa: BLE001
            result_queue.put(("err", exc))

    worker = threading.Thread(target=_worker, name=f"fetch-{label}", daemon=True)
    worker.start()
    try:
        kind, payload = result_queue.get(timeout=SOURCE_FETCH_TIMEOUT_SECONDS)
    except Empty as exc:
        # 外部传入的会话归调用方所有，不得 shutdown；自建会话则停掉，避免复用状态不明的会话。
        if owns_session and session is not None:
            try:
                session.shutdown()
            except Exception:  # noqa: BLE001
                pass
            stop_note = "已停止 bsk 会话并跳过该源"
        elif session is not None:
            # 会话归调用方所有：本层有句柄但无权停，交由调用方处理。
            stop_note = "会话归调用方所有，本层不停止该会话，直接跳过该源"
        else:
            # 本层没有会话句柄可停：HTTP 直连源（如 B站稍后看）压根没有会话；或会话归抓取器
            # 自建、已在上游超时中被自动停止。两种情况都停不了，也不该谎称停了。
            stop_note = "本层无会话句柄可停（HTTP 直连源，或会话归抓取器自建），直接跳过该源"
        raise _SourceFetchTimeout(
            f"{label}抓取超过 {SOURCE_FETCH_TIMEOUT_SECONDS}s 未完成，{stop_note}"
        ) from exc
    if kind == "err":
        raise payload  # type: ignore[misc]
    return payload  # type: ignore[return-value]


def _fetch_source(*, source, fetcher, db: DBManager, session=None, owns_session: bool = False):
    """抓取单个源并落库；返回会话（超时置空，调用方据此让后续源自建会话）。"""
    log_line(f"[3/6] 开始抓取{source.name}")
    try:
        fetched = _run_fetcher_with_timeout(source.name, fetcher, session, owns_session=owns_session)
        # 兼容尚未切到 FetchBatch 的抓取器：裸 list 无法证明拿到全量，一律按 complete=False。
        batch = (
            fetched
            if isinstance(fetched, FetchBatch)
            else FetchBatch(items=list(fetched), complete=False)
        )
        _save_items(db, batch.items)
        record_result(source.id, ok=True)
        if batch.items:
            log_line(f"[3/6] {source.name}抓取完成，新增/更新 {len(batch.items)} 条")
        else:
            log_line(f"[3/6][WARN] {source.name}抓取完成但 0 条，可能是登录态失效或页面结构变更，请核对")
        # 只有确认拿到完整列表才敢按「源里没有 = 已移除」判定，抓取半途而废一律不动。
        if source.finish_on_removed:
            if not batch.complete:
                # 不完整时静默跳过会让功能悄悄失效，必须留痕才能发现翻页/接口退化。
                log_line(f"[3/6][WARN] {source.name} 未拿到完整列表，跳过源内移除判定")
            else:
                finished_rows = db.mark_missing_finished(
                    source.id, {item["url"] for item in batch.items}
                )
                for row in finished_rows:
                    log_line(f"[3/6][自动完成] {_brief_title(row)} | {source.name} | 源内已移除")
                log_line(f"[3/6][自动完成] {source.name} 源内已移除 {len(finished_rows)} 条，已标记完成")
    except _SourceFetchTimeout as exc:
        log_line(f"[3/6] {source.name}抓取失败: {exc}")
        record_result(source.id, ok=False, error=str(exc))
        return None
    except Exception as exc:  # noqa: BLE001
        log_line(f"[3/6] {source.name}抓取失败: {exc}")
        record_result(source.id, ok=False, error=str(exc))
    return session


def _session_alive(session) -> bool:
    """bsk 会话存活判定。

    上游子进程超时在默认 stop_session_on_timeout=True 下会自动 stop() 并清空 session_id，
    但会话句柄仍在；若不判定存活，后续源复用这个死会话会连锁秒失败。
    """
    browser = getattr(session, "browser", None)
    return bool(getattr(browser, "session_id", None))


def _fetch_enabled_sources(*, args: argparse.Namespace, db: DBManager, session=None) -> bool:
    """按注册表顺序抓取四源（停用源跳过），返回是否调度了任一源。

    会话生命周期（bsk 语义）：外部传入 session 时只复用、绝不 shutdown；否则自建一个共享
    BskRunnerSession 供四源复用（一次 start/stop），构造或 start 失败则浏览器源各自记为失败并继续
    走完流程。任一源超时后把会话置空，后续源各自新建会话继续（fetcher 支持 session=None 自建）。
    每源结束后还做一次存活判定：上游内部超时可能已自动 stop 该会话，此时也置空让后续源新建。
    """
    own_session = session is None
    bsk_session = session
    start_failed = False
    if own_session:
        try:
            bsk_session = BskRunnerSession(start_url="about:blank")
            bsk_session.start()
        except Exception as exc:  # noqa: BLE001
            log_line(f"[2/6] bsk 会话启动失败，本轮各源均记为失败: {exc}")
            bsk_session = None
            start_failed = True

    scheduled = False
    try:
        for source in source_registry.SOURCES:
            if not getattr(args, source.flag, False):
                continue
            if not source_registry.is_enabled(source.id):
                log_line(f"[3/6] {source.name}已停用，跳过抓取")
                continue
            scheduled = True
            # globals() 解析保留单测 monkeypatch pipeline.<fetcher_name> 的打桩点
            fetcher = globals()[source.fetcher_name]
            if not source.needs_browser:
                # HTTP 直连源（B站稍后看走官方 API）：不碰浏览器，bsk 会话起不起得来都照抓。
                _fetch_source(source=source, fetcher=fetcher, db=db)
                continue
            if start_failed:
                log_line(f"[3/6] {source.name}抓取失败: bsk 会话启动失败，跳过抓取")
                record_result(source.id, ok=False, error="bsk 会话启动失败，跳过抓取")
                continue
            bsk_session = _fetch_source(
                source=source,
                fetcher=fetcher,
                db=db,
                session=bsk_session,
                owns_session=own_session and bsk_session is not None,
            )
            if bsk_session is not None and not _session_alive(bsk_session):
                log_line("[3/6] bsk 会话已失效（上游超时自动停止），后续源将新建会话")
                bsk_session = None
    finally:
        if own_session and bsk_session is not None:
            bsk_session.shutdown()
    return scheduled


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


def run(args: argparse.Namespace, session=None) -> int:
    load_project_env(ENV_PATH)
    log_line("[1/6] 初始化 laterhub")
    db = DBManager(DB_PATH)
    tagger = _load_tagger()
    if args.retry_failed:
        reset_count = db.reset_failed_to_pending()
        log_line(f"[补偿] 已将 failed 重置回 pending {reset_count} 条")
    log_line("[2/6] 开始抓取稍后读来源")
    scheduled = _fetch_enabled_sources(args=args, db=db, session=session)
    if not scheduled:
        log_line("[3/6] 本轮未启用任何抓取来源")
    _tag_pending_rows(db, tagger)
    return _push_pending_rows(db)


def run_main_flow(*, fetch_bilibili: bool = True, fetch_douyin: bool = True, fetch_xiaoheihe: bool = True, fetch_weread: bool = True, retry_failed: bool = False, session=None) -> LaterhubRunSummary:
    args = argparse.Namespace(
        retry_failed=retry_failed,
        fetch_bilibili=fetch_bilibili,
        fetch_douyin=fetch_douyin,
        fetch_xiaoheihe=fetch_xiaoheihe,
        fetch_weread=fetch_weread,
    )
    run(args, session=session)
    db = DBManager(DB_PATH)
    fetched_sources = sum(
        1
        for source in source_registry.SOURCES
        if getattr(args, source.flag, False) and source_registry.is_enabled(source.id)
    )
    pending_total = len(db.list_by_status("pending"))
    return LaterhubRunSummary(
        fetched_sources=fetched_sources,
        pending_total=pending_total,
        push_enabled=ENABLE_FEISHU_PUSH,
    )


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))

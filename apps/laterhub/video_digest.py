"""视频简报：把稍后阅读里的视频做成一篇简明扼要的介绍。

独立子流程，不进主流水线（apps/laterhub/pipeline.py）。链路：
B站 CC 字幕直取 → 下载视频 + videocaptioner bijian 转录 → GLM 视频理解兜底；
抖音无 CC 字幕：免签直链下载 → videocaptioner bijian 转录 → LLM 简报（无 GLM 兜底）。
LLM 基于材料产出简报 md 落 data/reports/，links 表记 digest_status。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from apps.laterhub.config import DATA_DIR, DB_PATH, ENV_PATH
from apps.laterhub.db import DBManager
from apps.laterhub.video_digest_llm import (
    DigestContent,
    VideoDigestLLM,
    build_digest_filename,
    render_digest_markdown,
    write_digest_report,
)
from connectors.bilibili import video as bili_video
from connectors.douyin.video import download_douyin_video

REPORTS_DIR = DATA_DIR / "reports"
TMP_DIR = DATA_DIR / "tmp" / "video_digest"
# 字幕喂给 LLM 的字符上限： glm-5.3-flash 上下文足够，但一小时视频的逐字稿可超 5 万字，
# 截断到 2 万字保预算，报告里标注"仅基于前部分内容"
SUBTITLE_MAX_CHARS = 20000
ASR_TIMEOUT_SECONDS = 1800
DOUYIN_BATCH_GAP_SECONDS = 15

DOUYIN_VIDEO_PREFIX = "douyin.com/video/"


@dataclass(slots=True)
class DigestOutcome:
    link_id: int
    url: str
    title: str
    status: str  # succeeded / failed / unsupported / skipped
    detail: str = ""


@dataclass(slots=True)
class DigestRunSummary:
    outcomes: list[DigestOutcome] = field(default_factory=list)
    reset_count: int = 0

    @property
    def ok_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "succeeded")

    @property
    def fail_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("failed", "unsupported"))


def _srt_to_text(srt_path: Path) -> str:
    """从 SRT 提取纯文本：丢序号行与时间轴行，保留文本行。"""
    lines: list[str] = []
    for raw in srt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


def _transcribe_with_videocaptioner(video_path: Path, workdir: Path) -> Path:
    """调用 videocaptioner 转录，返回 SRT 路径。外部 CLI 缺失/失败抛 RuntimeError。

    bijian（必剪）与 jianying（剪映）同源互备：bijian 对无 cookie 高频访问会限流
    （实测任务创建成功、查结果 412），换 jianying 通常即成，两引擎依次尝试。
    """
    exe = shutil.which("videocaptioner")
    if not exe:
        raise RuntimeError("未找到 videocaptioner 命令（uv tool install videocaptioner）")
    srt_path = workdir / "asr.srt"
    errors: list[str] = []
    for asr in ("bijian", "jianying"):
        srt_path.unlink(missing_ok=True)
        result = subprocess.run(
            [exe, "transcribe", str(video_path), "--asr", asr, "-q", "-o", str(srt_path)],
            capture_output=True,
            text=True,
            timeout=ASR_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and srt_path.exists():
            return srt_path
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        errors.append(f"{asr}: exit={result.returncode} {tail[-1] if tail else '无输出'}")
    raise RuntimeError(f"videocaptioner 转录失败（bijian/jianying 均失败）: {'; '.join(errors)}")


def _format_duration(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return "未知"
    minutes, sec = divmod(seconds, 60)
    return f"{minutes}分{sec:02d}秒"


def _source_label(source: str) -> str:
    return {"bilibili_watchlater": "B站稍后看", "douyin_favorites": "抖音收藏"}.get(source, source)


def _subtitle_source_label(subtitle_source: str) -> str:
    return {
        "bilibili_cc": "B站字幕",
        "videocaptioner_bijian": "语音转写",
        "glm_video": "AI 视频理解",
    }.get(subtitle_source, subtitle_source)


def _finalize(
    db: DBManager,
    link_id: int,
    row,
    digest: DigestContent,
    *,
    subtitle_source: str,
    truncated: bool,
    reports_dir: Path,
    owner: str | None,
    duration_seconds: int | None,
) -> Path:
    filename = build_digest_filename(str(row["url"]))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    markdown = render_digest_markdown(
        title=str(row["title"]),
        url=str(row["url"]),
        source=str(row["source"]),
        owner=owner,
        duration_seconds=duration_seconds,
        subtitle_source=subtitle_source,
        generated_at=now,
        digest=digest,
        truncated=truncated,
    )
    path = write_digest_report(reports_dir, filename, markdown)
    db.mark_digest_succeeded(link_id, digest_path=filename, completed_at=datetime.now().isoformat(timespec="seconds"))
    return path


def process_one(db: DBManager, row, *, llm: VideoDigestLLM, reports_dir: Path = REPORTS_DIR, keep_temp: bool = False) -> DigestOutcome:
    """处理单条：字幕策略链 → LLM 简报 → 落盘记状态。任何一步失败不抛出，记 failed。"""
    link_id, url = int(row["id"]), str(row["url"])
    title = str(row["title"])
    errors: list[str] = []
    workdir = TMP_DIR / str(link_id)

    # 抖音：无 CC 字幕，免签直链下载 → bijian 转录 → LLM 简报；GLM video_url 兜底不适用（直链时效短）
    if DOUYIN_VIDEO_PREFIX in url:
        step = "下载"
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            video_path, duration_seconds = download_douyin_video(url, dest_dir=workdir)
            step = "语音转写"
            srt_path = _transcribe_with_videocaptioner(video_path, workdir)
            subtitle_text = _srt_to_text(srt_path)
            truncated = len(subtitle_text) > SUBTITLE_MAX_CHARS
            if truncated:
                subtitle_text = subtitle_text[:SUBTITLE_MAX_CHARS]
            step = "生成简报"
            digest = llm.generate_from_subtitle(
                title=title, owner=None, subtitle_text=subtitle_text, truncated=truncated
            )
            path = _finalize(
                db,
                link_id,
                row,
                digest,
                subtitle_source="videocaptioner_bijian",
                truncated=truncated,
                reports_dir=reports_dir,
                owner=None,
                duration_seconds=duration_seconds,
            )
            return DigestOutcome(link_id, url, title, "succeeded", str(path))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"抖音{step}失败: {exc}")
            detail = "; ".join(errors)
            db.mark_digest_failed(link_id, error_message=detail[:500])
            return DigestOutcome(link_id, url, title, "failed", detail)
        finally:
            if not keep_temp and workdir.exists():
                shutil.rmtree(workdir, ignore_errors=True)

    if not bili_video.is_bilibili_video_url(url):
        db.mark_digest_unsupported(link_id, error_message=f"非视频链接或暂不支持的来源: {url[:120]}")
        return DigestOutcome(link_id, url, title, "unsupported", "非视频链接")

    try:
        try:
            meta = bili_video.get_bilibili_video_meta(url)
        except Exception as exc:  # noqa: BLE001
            db.mark_digest_failed(link_id, error_message=f"获取视频详情失败: {exc}")
            return DigestOutcome(link_id, url, title, "failed", f"meta: {exc}")

        # 1) B站 CC 字幕直取：免费、秒级、免下载
        subtitle_text, subtitle_source, truncated = "", "", False
        try:
            cc = bili_video.get_bilibili_cc_subtitle_text(meta.bvid, meta.cid)
        except Exception as exc:  # noqa: BLE001
            cc = None
            errors.append(f"CC字幕接口失败: {exc}")
        if cc:
            subtitle_text, subtitle_source = cc, "bilibili_cc"
        else:
            # 2) 下载视频 + videocaptioner bijian 转录
            try:
                workdir.mkdir(parents=True, exist_ok=True)
                video_path = bili_video.download_bilibili_video(meta.bvid, meta.cid, dest_dir=workdir)
                srt_path = _transcribe_with_videocaptioner(video_path, workdir)
                subtitle_text = _srt_to_text(srt_path)
                subtitle_source = "videocaptioner_bijian"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"下载+ASR失败: {exc}")
                # 3) GLM 视频理解兜底：完整 mp4 直链直接喂给多模态模型
                try:
                    mp4_url = bili_video.get_bilibili_full_mp4_url(meta.bvid, meta.cid)
                except Exception as exc2:  # noqa: BLE001
                    mp4_url = None
                    errors.append(f"拿直链失败: {exc2}")
                if not mp4_url:
                    detail = "; ".join(errors) or "无可用字幕来源"
                    db.mark_digest_failed(link_id, error_message=detail[:500])
                    return DigestOutcome(link_id, url, title, "failed", detail)
                digest = llm.generate_from_video_url(title=title, owner=meta.owner, video_url=mp4_url)
                path = _finalize(db, link_id, row, digest, subtitle_source="glm_video", truncated=False, reports_dir=reports_dir, owner=meta.owner, duration_seconds=meta.duration_seconds)
                return DigestOutcome(link_id, url, title, "succeeded", str(path))

        if len(subtitle_text) > SUBTITLE_MAX_CHARS:
            subtitle_text = subtitle_text[:SUBTITLE_MAX_CHARS]
            truncated = True
        digest = llm.generate_from_subtitle(
            title=title, owner=meta.owner, subtitle_text=subtitle_text, truncated=truncated
        )
        path = _finalize(db, link_id, row, digest, subtitle_source=subtitle_source, truncated=truncated, reports_dir=reports_dir, owner=meta.owner, duration_seconds=meta.duration_seconds)
        return DigestOutcome(link_id, url, title, "succeeded", str(path))
    except Exception as exc:  # noqa: BLE001
        db.mark_digest_failed(link_id, error_message=f"{type(exc).__name__}: {exc}"[:500])
        return DigestOutcome(link_id, url, title, "failed", str(exc))
    finally:
        if not keep_temp and workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)


def run_video_digest(
    *,
    db_path: str | Path | None = None,
    limit: int = 1,
    link_id: int | None = None,
    source: str | None = None,
    retry_failed: bool = False,
    retry_unsupported: bool = False,
    dry_run: bool = False,
    keep_temp: bool = False,
    env_path: Path | None = None,
) -> DigestRunSummary:
    from apps.laterhub.tagger import LLMConfig, load_project_env

    load_project_env(env_path or ENV_PATH)
    db = DBManager(db_path or DB_PATH)
    db.recover_stale_digest_runs()
    reset_count = db.reset_unsupported_to_pending() if retry_unsupported else 0

    summary = DigestRunSummary(reset_count=reset_count)
    if link_id is not None:
        row = db.get_digest_row(link_id)
        if row is None:
            summary.outcomes.append(DigestOutcome(link_id, "", "", "failed", "条目不存在"))
            return summary
        # succeeded 但报告文件已丢（目录被清理/迁移）：降回 failed 让本次重试能领取，否则按钮永久死锁
        digest_file = str(row["digest_path"] or "").strip()
        if str(row["digest_status"]) == "succeeded" and (not digest_file or not (REPORTS_DIR / digest_file).is_file()):
            db.mark_digest_failed(link_id, error_message="报告文件缺失，已重新排队")
            row = db.get_digest_row(link_id)
        if not db.claim_digest(link_id, retry_failed=True):
            status = str(row["digest_status"])
            summary.outcomes.append(DigestOutcome(link_id, str(row["url"]), str(row["title"]), "skipped", f"状态 {status} 不可领取"))
            return summary
        rows = [db.get_digest_row(link_id)]
    else:
        rows = db.list_digest_candidates(limit=limit, source=source, retry_failed=retry_failed)

    if dry_run:
        for row in rows:
            summary.outcomes.append(DigestOutcome(int(row["id"]), str(row["url"]), str(row["title"]), "skipped", "dry-run"))
        return summary

    primary = LLMConfig.from_env(env_path or ENV_PATH)
    try:
        backup = LLMConfig.from_env(env_path or ENV_PATH, backup=True)
    except Exception:  # noqa: BLE001
        backup = None
    llm = VideoDigestLLM(primary, backup)

    previous_douyin = False
    for index, row in enumerate(rows):
        if row is None:
            continue
        is_douyin = DOUYIN_VIDEO_PREFIX in str(row["url"])
        # 同会话密集访问抖音视频页会触发风控（页面不出播放器），批内抖音条目之间留间隔
        if is_douyin and index > 0 and previous_douyin:
            time.sleep(DOUYIN_BATCH_GAP_SECONDS)
        outcome = process_one(db, row, llm=llm, keep_temp=keep_temp)
        summary.outcomes.append(outcome)
        previous_douyin = is_douyin
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="视频简报：稍后阅读视频 → 字幕 → 简明介绍")
    parser.add_argument("--limit", type=int, default=1, help="最多处理 N 条（默认 1）")
    parser.add_argument("--id", type=int, default=None, help="指定 laterhub 条目 id")
    parser.add_argument("--source", default=None, help="按来源过滤（如 bilibili_watchlater）")
    parser.add_argument("--retry-failed", action="store_true", help="连同 failed 条目一起重试")
    parser.add_argument("--retry-unsupported", action="store_true", help="把 unsupported 视频条目重置为 pending 后重试")
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理条目")
    parser.add_argument("--keep-temp", action="store_true", help="保留下载的临时文件（调试用）")
    parser.add_argument("--db", default=None, help="指定 sqlite 路径（默认 data/laterhub.sqlite3）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_video_digest(
        db_path=args.db,
        limit=max(1, args.limit),
        link_id=args.id,
        source=args.source,
        retry_failed=args.retry_failed,
        retry_unsupported=args.retry_unsupported,
        dry_run=args.dry_run,
        keep_temp=args.keep_temp,
    )
    if summary.reset_count:
        print(f"已重置 {summary.reset_count} 条 unsupported 视频为 pending")
    for o in summary.outcomes:
        marker = {"succeeded": "OK", "failed": "FAIL", "unsupported": "SKIP", "skipped": "SKIP"}.get(o.status, o.status.upper())
        print(f"[{marker}] #{o.link_id} {o.title[:40]} {o.detail}")
    print(f"完成: 成功 {summary.ok_count} / 失败 {summary.fail_count} / 共 {len(summary.outcomes)}")
    return 0 if summary.fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

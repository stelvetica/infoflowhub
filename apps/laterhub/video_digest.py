"""视频简报：把稍后阅读里的视频做成一篇简明扼要的介绍。

独立子流程，不进主流水线（apps/laterhub/pipeline.py）。链路：
B站（bsk 页内取数，一个条目一个会话）：CC 字幕直取 → 下载视频 + videocaptioner bijian
转录 → GLM 视频理解兜底；
抖音：作品自带字幕优先（用户脚本标记里的 subtitle_infos）→ 没有才下载 + videocaptioner 转录 → LLM 简报（无 GLM 兜底）。
LLM 基于材料产出简报 md 落 data/reports/，links 表记 digest_status。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

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
from connectors.douyin.video import download_douyin_video, fetch_douyin_subtitle_text

REPORTS_DIR = DATA_DIR / "reports"
# 简报产物独立子目录：不与其它产物混放，也不进面板「新闻公告」白名单
DIGEST_SUBDIR = "video_digest"
TMP_DIR = DATA_DIR / "tmp" / "video_digest"
# 长字幕不在编排层截断：video_digest_llm 按行分批整理后再合并（见 CHUNK_MAX_CHARS）。
ASR_TIMEOUT_SECONDS = 1800
DOUYIN_BATCH_GAP_SECONDS = 15
# 无人声时的画面理解：均匀抽帧数。小红书式竖屏字幕一般在画面上，4 张足够覆盖片头与主体。
FRAME_COUNT = 4
FRAME_MAX_WIDTH = 512
FRAME_EXTRACT_TIMEOUT_SECONDS = 120

DOUYIN_VIDEO_PREFIX = "douyin.com/video/"


class NoSpeechDetected(RuntimeError):
    """两个 ASR 引擎都跑通、但结果为空/过短：这条视频本身没有人声（纯音乐、画面剪辑）。"""


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
    return _subtitle_lines_to_text(srt_path.read_text(encoding="utf-8", errors="replace"))


def _subtitle_lines_to_text(raw: str) -> str:
    """把 SRT/WebVTT 正文变成纯文本；WebVTT 的头部行（WEBVTT/NOTE/STYLE）一并丢掉。"""
    lines: list[str] = []
    for raw_line in (raw or "").splitlines():
        line = raw_line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        if line.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        lines.append(re.sub(r"<[^>]+>", "", line))
    return "\n".join(lines)


def _subtitle_to_text(raw: str) -> str:
    """抖音自带字幕原文 → 纯文本：兼容 JSON / SRT / WebVTT 三种格式。

    抖音的 subtitle_infos 指向的字幕有时是 JSON（items 里带 text），有时是 SRT/VTT；
    格式判断失败时按 SRT 处理，宁可少字也不让整条简报失败。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            items = data if isinstance(data, list) else (
                data.get("utterances") or data.get("captions") or data.get("body")
                or data.get("subtitles") or data.get("lines") or []
            )
            if isinstance(items, str):
                items = [{"text": items}]
            lines = [str(x.get("text") or x.get("content") or "").strip() for x in items if isinstance(x, dict)]
            joined = "\n".join(line for line in lines if line)
            if joined.strip():
                return joined
    return _subtitle_lines_to_text(text)


def _asr_min_chars(duration_seconds: int | None) -> int:
    """判定「有效语音」的最低字数：按 0.3 字/秒留余量，低于此视为无人声/仅 BGM。

    0.3 字/秒远低于正常语速（中文约 3~5 字/秒），只用来拦住空结果与纯歌词，
    不误杀慢速讲解；时长未知时只要非空即可（≥3 字）。
    """
    if duration_seconds and duration_seconds > 0:
        return max(3, int(duration_seconds * 0.3))
    return 3


def _transcribe_with_videocaptioner(
    video_path: Path, workdir: Path, *, duration_seconds: int | None = None
) -> Path:
    """调用 videocaptioner 转录，返回有效 SRT 路径。

    CLI 缺失/引擎报错抛 RuntimeError；两个引擎都跑通却只拿到空结果时抛 NoSpeechDetected
    （说明视频本来就没人声，调用方可以换文案兜底）。

    bijian（必剪）与 jianying（剪映）同源互备：bijian 对无 cookie 高频访问会限流
    （实测任务创建成功、查结果 412），换 jianying 通常即成，两引擎依次尝试。
    空 SRT / 仅 BGM 的极短结果不算成功，必须继续试下一个引擎——上游会以 exit=0
    返回 0 字节文件（2026-11 实测），只看 returncode 会把空结果当成功。
    """
    exe = shutil.which("videocaptioner")
    if not exe:
        raise RuntimeError("未找到 videocaptioner 命令（uv tool install videocaptioner）")
    srt_path = workdir / "asr.srt"
    min_chars = _asr_min_chars(duration_seconds)
    errors: list[str] = []
    only_short_results = True
    for asr in ("bijian", "jianying"):
        srt_path.unlink(missing_ok=True)
        result = subprocess.run(
            [exe, "transcribe", str(video_path), "--asr", asr, "-q", "-o", str(srt_path)],
            capture_output=True,
            text=True,
            timeout=ASR_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and srt_path.exists():
            text = _srt_to_text(srt_path)
            if len(text) >= min_chars:
                return srt_path
            errors.append(f"{asr}: 结果过短({len(text)}字，需≥{min_chars})")
            continue
        only_short_results = False
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        errors.append(f"{asr}: exit={result.returncode} {tail[-1] if tail else '无输出'}")
    message = f"videocaptioner 未识别到有效语音（bijian/jianying 均失败）: {'; '.join(errors)}"
    # 两个引擎都跑通、只是结果是空/过短 → 判定「本来就没有人声」，可换文案兜底；
    # 有引擎报错(exit≠0) 说明工具链坏了，属于真失败，不能拿文案顶替字幕。
    raise (NoSpeechDetected(message) if only_short_results else RuntimeError(message))


def _split_douyin_caption(title: str) -> tuple[str | None, str]:
    """把抖音标题拆成 (作者, 文案)。抖音采集的标题形如「作者：文案 #话题」。

    拿不准就不拆（作者 None，整条当文案）——拆错只会让简报里多个错误作者名。
    """
    author, sep, caption = title.partition("：")
    if sep and 0 < len(author.strip()) <= 20 and caption.strip():
        return author.strip(), caption.strip()
    return None, title


def _extract_frames(video_path: Path, duration_seconds: int | None, workdir: Path, *, count: int = FRAME_COUNT) -> list[Path]:
    """按时间均匀抽 count 张截图（无人声时给 LLM 看图）。

    取每段中点而不是首尾：片头/片尾常是黑场或片尾卡，中点更能代表内容。
    时长未知或 ffmpeg 缺失时返回空表，由调用方退回文案通道。
    """
    exe = shutil.which("ffmpeg")
    if not exe or not duration_seconds or duration_seconds <= 0:
        return []
    frames: list[Path] = []
    for index in range(count):
        timestamp = duration_seconds * (index + 0.5) / count
        target = workdir / f"frame_{index}.jpg"
        try:
            subprocess.run(
                [
                    exe, "-y", "-v", "error",
                    "-ss", f"{timestamp:.2f}",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", f"scale={FRAME_MAX_WIDTH}:-2",
                    str(target),
                ],
                capture_output=True,
                timeout=FRAME_EXTRACT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if target.exists() and target.stat().st_size > 0:
            frames.append(target)
    return frames


def _finalize(
    db: DBManager,
    link_id: int,
    row,
    digest: DigestContent,
    *,
    subtitle_source: str,
    reports_dir: Path,
    owner: str | None,
    duration_seconds: int | None,
    process_note: str = "",
    source_text: str = "",
) -> Path:
    url = str(row["url"])
    filename = build_digest_filename(str(row["title"]), url)
    digest_dir = reports_dir / DIGEST_SUBDIR
    # 不同视频同标题会互相覆盖（原子写是替换语义）：目标已存在且不属于本条目时，追加 url 短哈希
    if (digest_dir / filename).exists() and str(row["digest_path"] or "") != f"{DIGEST_SUBDIR}/{filename}":
        stem = filename[:-3]
        filename = f"{stem}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:8]}.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    markdown = render_digest_markdown(
        title=str(row["title"]),
        url=url,
        source=str(row["source"]),
        owner=owner,
        duration_seconds=duration_seconds,
        subtitle_source=subtitle_source,
        generated_at=now,
        digest=digest,
        process_note=process_note,
        source_text=source_text,
    )
    # digest_path 存相对 reports 根的路径（含子目录），面板/幂等检查按此定位
    path = write_digest_report(digest_dir, filename, markdown)
    db.mark_digest_succeeded(link_id, digest_path=f"{DIGEST_SUBDIR}/{filename}", completed_at=datetime.now().isoformat(timespec="seconds"))
    return path


def process_one(db: DBManager, row, *, llm: VideoDigestLLM, reports_dir: Path = REPORTS_DIR, keep_temp: bool = False) -> DigestOutcome:
    """处理单条：字幕策略链 → LLM 简报 → 落盘记状态。任何一步失败不抛出，记 failed。"""
    link_id, url = int(row["id"]), str(row["url"])
    title = str(row["title"])
    errors: list[str] = []
    workdir = TMP_DIR / str(link_id)

    # 抖音：作品自带字幕优先 → 没有才 免签直链下载 → bijian 转录 → LLM 简报；GLM video_url 兜底不适用（直链时效短）
    if DOUYIN_VIDEO_PREFIX in url:
        step = "下载"
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            resolved: dict[str, Any] = {}
            video_path, duration_seconds = download_douyin_video(url, dest_dir=workdir, resolved=resolved)
            media = resolved.get("media")
            # 自带字幕优先：字幕轨是现成文本，跳过整段语音转写（批量解析里最贵的一步）。
            # 下载仍然做——用户要保留视频文件；只有字幕不可用时才走 ASR。
            subtitle_text = ""
            for subtitle_url in getattr(media, "subtitle_urls", None) or []:
                try:
                    raw_subtitle = fetch_douyin_subtitle_text(subtitle_url)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"自带字幕下载失败: {exc}")
                    continue
                candidate = _subtitle_to_text(raw_subtitle)
                if len(candidate) >= _asr_min_chars(duration_seconds):
                    subtitle_text = candidate
                    break
                errors.append(f"自带字幕过短({len(candidate)}字)")
            if subtitle_text:
                step = "生成简报"
                digest = llm.generate_from_subtitle(title=title, owner=None, subtitle_text=subtitle_text)
                path = _finalize(
                    db,
                    link_id,
                    row,
                    digest,
                    subtitle_source="douyin_subtitle",
                    reports_dir=reports_dir,
                    owner=None,
                    duration_seconds=duration_seconds,
                    process_note="使用作品自带字幕（跳过语音转写）。",
                    source_text=subtitle_text,
                )
                return DigestOutcome(link_id, url, title, "succeeded", str(path))
            step = "语音转写"
            try:
                subtitle_text = _srt_to_text(
                    _transcribe_with_videocaptioner(video_path, workdir, duration_seconds=duration_seconds)
                )
            except NoSpeechDetected as exc:
                # 无人声：先看画面（这类剪辑常有字幕卡/文字），看图失败再退回作品文案
                owner, caption = _split_douyin_caption(title)
                digest = None
                frames = _extract_frames(video_path, duration_seconds, workdir)
                if frames:
                    step = "画面理解"
                    try:
                        digest = llm.generate_from_frames(
                            title=title,
                            owner=owner,
                            frames=[frame.read_bytes() for frame in frames],
                            description=caption,
                        )
                        subtitle_source = "douyin_frames"
                        process_note = f"整段音频未识别到人声（{exc}），改用 {len(frames)} 张截图做画面理解。"
                    except Exception as frame_exc:  # noqa: BLE001
                        # 看图失败不该让整条失败：降级到作品文案，并把原因留在处理说明里
                        errors.append(f"画面理解失败: {frame_exc}")
                        digest = None
                if digest is None:
                    step = "生成简报"
                    subtitle_source = "douyin_desc"
                    process_note = f"整段音频未识别到人声（{exc}），简报依据作品文案生成。"
                    digest = llm.generate_from_description(title=title, owner=owner, description=caption)
                path = _finalize(
                    db,
                    link_id,
                    row,
                    digest,
                    subtitle_source=subtitle_source,
                    reports_dir=reports_dir,
                    owner=owner,
                    duration_seconds=duration_seconds,
                    process_note=process_note,
                    source_text=caption,
                )
                return DigestOutcome(link_id, url, title, "succeeded", str(path))
            step = "生成简报"
            digest = llm.generate_from_subtitle(title=title, owner=None, subtitle_text=subtitle_text)
            path = _finalize(
                db,
                link_id,
                row,
                digest,
                subtitle_source="videocaptioner_bijian",
                reports_dir=reports_dir,
                owner=None,
                duration_seconds=duration_seconds,
                source_text=subtitle_text,
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

    # B站全部走 bsk：一个条目一个会话，同一个 page 承载 view/字幕/直链/下载。
    from connectors._shared.bsk_session import BskRunnerSession

    bsk_session = None
    try:
        try:
            bsk_session = BskRunnerSession(start_url="about:blank")
            bsk_session.start()
        except Exception as exc:  # noqa: BLE001
            db.mark_digest_failed(link_id, error_message=f"bsk 会话启动失败: {exc}"[:500])
            return DigestOutcome(link_id, url, title, "failed", f"bsk: {exc}")
        page = bsk_session.acquire_page()
        try:
            meta = bili_video.get_bilibili_video_meta(url, page=page)
        except Exception as exc:  # noqa: BLE001
            db.mark_digest_failed(link_id, error_message=f"获取视频详情失败: {exc}")
            return DigestOutcome(link_id, url, title, "failed", f"meta: {exc}")

        # 1) B站 CC 字幕直取：免费、秒级、免下载
        subtitle_text, subtitle_source = "", ""
        work_notes: list[str] = []
        try:
            cc = bili_video.get_bilibili_cc_subtitle_text(meta.bvid, meta.cid, page=page)
        except Exception as exc:  # noqa: BLE001
            cc = None
            errors.append(f"CC字幕接口失败: {exc}")
            work_notes.append(f"CC字幕接口异常: {exc}")
        if cc and len(cc) >= _asr_min_chars(meta.duration_seconds):
            subtitle_text, subtitle_source = cc, "bilibili_cc"
        else:
            if cc:
                work_notes.append(f"CC字幕过短({len(cc)}字)，视为无有效内容")
            else:
                work_notes.append("CC字幕未取到")
            # 2) 下载视频 + videocaptioner 转录
            try:
                workdir.mkdir(parents=True, exist_ok=True)
                video_path = bili_video.download_bilibili_video(meta.bvid, meta.cid, page=page, dest_dir=workdir)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"下载视频失败: {exc}")
                work_notes.append(f"下载视频失败: {exc}")
            else:
                work_notes.append("已下载视频")
                try:
                    srt_path = _transcribe_with_videocaptioner(video_path, workdir, duration_seconds=meta.duration_seconds)
                    subtitle_text = _srt_to_text(srt_path)
                    subtitle_source = "videocaptioner_bijian"
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"语音转写失败: {exc}")
                    work_notes.append("转写未识别到有效人声（bijian/jianying）")

        if subtitle_text:
            # 正常路径也把材料链写清楚：简介参与了就明确标注，避免用户不知道是否用了简介
            material_notes = ["使用B站CC字幕"] if subtitle_source == "bilibili_cc" else [*work_notes, "语音转写成功"]
            if meta.description:
                material_notes.append("B站简介作为补充材料参与分析")
            digest = llm.generate_from_subtitle(
                title=title,
                owner=meta.owner,
                subtitle_text=subtitle_text,
                description=meta.description,
            )
            path = _finalize(
                db, link_id, row, digest,
                subtitle_source=subtitle_source,
                reports_dir=reports_dir,
                owner=meta.owner,
                duration_seconds=meta.duration_seconds,
                process_note="；".join(material_notes),
                source_text=subtitle_text,
            )
            return DigestOutcome(link_id, url, title, "succeeded", str(path))

        # 3) 无人声：优先用 B站简介分析；简介为空才回退多模态看视频
        if meta.description:
            work_notes.append("改用B站简介分析")
            process_note = "；".join(work_notes)[:500]
            digest = llm.generate_from_description(title=title, owner=meta.owner, description=meta.description)
            path = _finalize(
                db, link_id, row, digest,
                subtitle_source="bilibili_desc",
                reports_dir=reports_dir,
                owner=meta.owner,
                duration_seconds=meta.duration_seconds,
                process_note=process_note,
                source_text=meta.description,
            )
            return DigestOutcome(link_id, url, title, "succeeded", str(path))

        work_notes.append("B站简介为空，改用AI视频理解")
        process_note = "；".join(work_notes)[:500]
        # 4) GLM 视频理解兜底：完整 mp4 直链直接喂给多模态模型
        try:
            mp4_url = bili_video.get_bilibili_full_mp4_url(meta.bvid, meta.cid, page=page)
        except Exception as exc2:  # noqa: BLE001
            mp4_url = None
            errors.append(f"拿直链失败: {exc2}")
        if not mp4_url:
            detail = "; ".join(errors) or "无可用字幕来源"
            db.mark_digest_failed(link_id, error_message=detail[:500])
            return DigestOutcome(link_id, url, title, "failed", detail)
        digest = llm.generate_from_video_url(title=title, owner=meta.owner, video_url=mp4_url, description=meta.description)
        path = _finalize(
            db, link_id, row, digest,
            subtitle_source="glm_video",
            reports_dir=reports_dir,
            owner=meta.owner,
            duration_seconds=meta.duration_seconds,
            process_note=process_note,
        )
        return DigestOutcome(link_id, url, title, "succeeded", str(path))
    except Exception as exc:  # noqa: BLE001
        db.mark_digest_failed(link_id, error_message=f"{type(exc).__name__}: {exc}"[:500])
        return DigestOutcome(link_id, url, title, "failed", str(exc))
    finally:
        if bsk_session is not None:
            bsk_session.shutdown()
        if not keep_temp and workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)


def run_video_digest(
    *,
    db_path: str | Path | None = None,
    limit: int = 1,
    link_id: int | None = None,
    source: str | None = None,
    tag: str | None = None,
    retry_failed: bool = False,
    retry_unsupported: bool = False,
    dry_run: bool = False,
    keep_temp: bool = False,
    env_path: Path | None = None,
    claimed: bool = False,
) -> DigestRunSummary:
    """claimed=True 供 Web 端使用：路由已原子领取（pending/failed→running），这里不再重复 claim。"""
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
        if not claimed and not db.claim_digest(link_id, retry_failed=True):
            status = str(row["digest_status"])
            summary.outcomes.append(DigestOutcome(link_id, str(row["url"]), str(row["title"]), "skipped", f"状态 {status} 不可领取"))
            return summary
        rows = [db.get_digest_row(link_id)]
    else:
        rows = db.list_digest_candidates(limit=limit, source=source, tag=tag, retry_failed=retry_failed)

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
    parser.add_argument("--tag", default=None, help="按标签过滤（如 技术_AI/工具/开发），只处理打了该标签的视频")
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
        tag=args.tag,
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

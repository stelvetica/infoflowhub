"""视频简报的 LLM 摘要与报告渲染。

编排层（apps/laterhub/video_digest.py）负责字幕获取策略链与落盘调度；本模块只做两件事：
1. 把视频材料（字幕文本 / 视频直链）交给主备两家 LLM，产出结构化 DigestContent；
2. 把 DigestContent 渲染成仓库 md 渲染器子集能吃的 Markdown 报告并原子落盘。

主备降级口径与 ContentTagger 一致：主家 HTTP 错/畸形 JSON/空 content/校验失败都换备用，
两家都失败才抛 RuntimeError（错误信息含两家摘要）。不区分永久/可重试——编排层只记 failed。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from apps.laterhub.tagger import LLMConfig


# 长字幕强制分批（对齐 explain-notes 的「长文本必须分批」原则）：不截断、不跳读。
# 每块按行切到 12k 字以内；相邻块保留 2 行重叠，避免在句子/论点边界处断链。
CHUNK_MAX_CHARS = 12000
CHUNK_OVERLAP_LINES = 2
# 分段笔记合并上限：超过则再做一层归并，保证最终请求不会撑爆上下文。
MERGE_MAX_CHARS = 24000

SYSTEM_PROMPT = """你是视频内容简报助手。你的任务是把给定的视频材料整理成一份简明扼要的中文简报。

硬约束：
1. 只基于给定材料（字幕文本 / 视频本身）总结，禁止编造材料中不存在的事实、数字、人物或结论。
2. 只能输出一个 JSON 对象，禁止输出解释、Markdown、代码块。
3. JSON 结构固定为：{"summary": "...", "key_points": ["...", "..."], "note": "可选"}。
4. summary：一段话，80 字以内，简明扼要概括视频主旨。
5. key_points：3-7 条，每条一句话，覆盖视频的核心信息与结论。
6. note：可选字符串；材料不完整、来源可疑或无法确认时在此说明，无话可说填 null。
"""

SUBTITLE_HINT = """
补充要求：材料是视频语音转写字幕，可能存在同音字错误与断句问题。请理解语义而非逐字照搬，
不要复述错别字，也不要因为个别字词错误而偏离视频真实意思。
"""

CHUNK_SYSTEM_PROMPT = """你是视频字幕的分段整理器。你的任务是把给定的一段字幕按时间顺序整理成信息完整的分段笔记。

硬约束：
1. 只基于给定片段整理，禁止编造片段中不存在的事实、数字、人物或结论。
2. 输出纯文本，不要 JSON、不要 Markdown 代码块。
3. 保留本段的观点、论证、案例、数据、时间、条件与例外；不能只留结论、丢掉论据。
4. 按时间顺序分段，可用短横线列表；关键名词与核心数据加粗。
5. 字幕可能有同音字与断句错误：按语义理解，不要复述错别字，也不要因此偏离原意。
"""

MERGE_HINT = """
补充要求：材料是同一个视频按时间顺序分段整理后的笔记（不是原文字幕）。请合并成全局简报：
- 跨段重复的信息去重，但不得丢掉后段新增的数据、条件、例外与结论；
- 按视频推进顺序组织，保留前后因果、转折与最终结论；
- 只基于这些分段笔记，不引入外部信息。
"""

VIDEO_URL_HINT = """
补充要求：材料是视频本身（多模态输入），请直接观看视频内容后按要求总结。
"""


@dataclass(slots=True)
class DigestContent:
    summary: str
    key_points: list[str]
    note: str | None


def split_text(
    text: str,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_lines: int = CHUNK_OVERLAP_LINES,
) -> list[str]:
    """按行切块（单行超长时硬切），相邻块保留若干行重叠，避免断句处丢上下文。

    只做机械切分，不改写正文；所有行都会进入某个块，保证零遗漏。
    """
    lines: list[str] = []
    for line in str(text).splitlines():
        if len(line) <= max_chars:
            lines.append(line)
        else:
            lines.extend(line[start : start + max_chars] for start in range(0, len(line), max_chars))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(current))
            current = current[-overlap_lines:] if overlap_lines > 0 else []
            # 重叠行本身不能把下一块撑爆（超长单行被硬切时会遇到）
            while current and sum(len(item) + 1 for item in current) + len(line) + 1 > max_chars:
                current = current[1:]
            size = sum(len(item) + 1 for item in current)
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


class VideoDigestLLM:
    def __init__(
        self,
        config: LLMConfig,
        backup_config: LLMConfig | None = None,
        *,
        timeout: float = 180,
    ) -> None:
        self.config = config
        self.backup_config = backup_config
        self.timeout = timeout

    def generate_from_subtitle(
        self,
        *,
        title: str,
        owner: str | None,
        subtitle_text: str,
    ) -> DigestContent:
        """字幕 → 简报。长字幕分批整理后合并，不截断、不跳读。"""
        chunks = split_text(subtitle_text)
        if len(chunks) <= 1:
            system = SYSTEM_PROMPT + SUBTITLE_HINT
            user = (
                f"视频标题：{title}\n"
                f"作者：{owner or '未知'}\n\n"
                f"字幕文本：\n{subtitle_text}"
            )
            return self._with_fallback(system, user)

        notes = [
            self._summarize_chunk(title=title, owner=owner, chunk=chunk, index=index, total=len(chunks))
            for index, chunk in enumerate(chunks, 1)
        ]
        return self._merge_notes(title=title, owner=owner, notes=notes)

    def _summarize_chunk(
        self, *, title: str, owner: str | None, chunk: str, index: int, total: int
    ) -> str:
        user = (
            f"视频标题：{title}\n"
            f"作者：{owner or '未知'}\n"
            f"本段是第 {index}/{total} 段字幕。\n\n"
            f"字幕片段：\n{chunk}"
        )
        return self._text_with_fallback(CHUNK_SYSTEM_PROMPT, user)

    def _merge_notes(self, *, title: str, owner: str | None, notes: list[str]) -> DigestContent:
        merged = self._join_notes(notes)
        # 极长视频：分段笔记仍可能超出单次合并预算，再做一层归并（最多 3 轮兜底）
        for _ in range(3):
            if len(merged) <= MERGE_MAX_CHARS:
                break
            groups = split_text(merged, max_chars=CHUNK_MAX_CHARS, overlap_lines=0)
            notes = [
                self._summarize_chunk(title=title, owner=owner, chunk=group, index=index, total=len(groups))
                for index, group in enumerate(groups, 1)
            ]
            merged = self._join_notes(notes)
        system = SYSTEM_PROMPT + MERGE_HINT
        user = (
            f"视频标题：{title}\n"
            f"作者：{owner or '未知'}\n\n"
            f"分段整理：\n{merged}"
        )
        return self._with_fallback(system, user)

    @staticmethod
    def _join_notes(notes: list[str]) -> str:
        return "\n\n".join(f"【第 {index} 段】\n{note}" for index, note in enumerate(notes, 1))

    def generate_from_video_url(
        self,
        *,
        title: str,
        owner: str | None,
        video_url: str,
    ) -> DigestContent:
        system = SYSTEM_PROMPT + VIDEO_URL_HINT
        prompt = (
            f"视频标题：{title}\n"
            f"作者：{owner or '未知'}\n"
            "请直接观看视频，并严格按要求只返回 JSON 简报对象。"
        )
        # 多模态：文本指令 + 视频直链（R4M 端点已实测支持 video_url）
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {"type": "video_url", "video_url": {"url": video_url}},
        ]
        return self._with_fallback(system, content)

    def _with_fallback(self, system: str, user_content: Any) -> DigestContent:
        attempts: list[tuple[str, LLMConfig]] = [("primary", self.config)]
        if self.backup_config:
            attempts.append(("backup", self.backup_config))
        errors: list[str] = []
        for label, config in attempts:
            try:
                return self._generate(config=config, system=system, user_content=user_content)
            except Exception as exc:  # noqa: BLE001
                errors.append(self._format_error(label, exc))
        raise RuntimeError("视频简报生成失败: " + "; ".join(errors))

    def _text_with_fallback(self, system: str, user: str, *, max_tokens: int = 3000) -> str:
        """分段整理走纯文本输出（不要求 JSON），主备降级口径与简报一致。"""
        attempts: list[tuple[str, LLMConfig]] = [("primary", self.config)]
        if self.backup_config:
            attempts.append(("backup", self.backup_config))
        errors: list[str] = []
        for label, config in attempts:
            try:
                return self._complete(
                    config=config,
                    system=system,
                    user_content=user,
                    json_mode=False,
                    max_tokens=max_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(self._format_error(label, exc))
        raise RuntimeError("字幕分段整理失败: " + "; ".join(errors))

    def _generate(self, *, config: LLMConfig, system: str, user_content: Any) -> DigestContent:
        content = self._complete(
            config=config,
            system=system,
            user_content=user_content,
            json_mode=True,
            max_tokens=3000,
        )
        return self._validate(self._parse_json_object(content))

    def _complete(
        self,
        *,
        config: LLMConfig,
        system: str,
        user_content: Any,
        json_mode: bool,
        max_tokens: int,
    ) -> str:
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0.2,
            # glm-5.3-flash 等思考模型把思考 token 计入 max_tokens 且不受 thinking 参数约束，
            # 上限过小会整条返回空 content（与 tagger.py 同一坑，故这里给到 3000）。
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = requests.post(
            f"{config.base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        message = ((data.get("choices") or [{}])[0].get("message") or {})
        # content 可能是 null（思考模型把 max_tokens 全用在思考上时实测如此）：给出可读错误，
        # 不能退化成 'NoneType' object has no attribute 'strip'。
        content = str(message.get("content") or "").strip()
        if not content:
            raise RuntimeError("模型返回空 content（思考模型可能耗尽 max_tokens）")
        return content

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise ValueError(f"无法解析模型返回的 JSON: {text[:200]}") from None
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise ValueError(f"无法解析模型返回的 JSON: {text[:200]}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"模型返回不是 JSON 对象: {text[:200]}")
        return parsed

    @staticmethod
    def _validate(obj: dict[str, Any]) -> DigestContent:
        summary = obj.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary 缺失或为空")

        raw_points = obj.get("key_points")
        if not isinstance(raw_points, list):
            raise ValueError("key_points 不是数组")
        points = [item.strip() for item in raw_points if isinstance(item, str) and item.strip()]
        if len(points) < 3:
            raise ValueError(f"key_points 少于 3 条: {len(points)}")
        points = points[:7]

        raw_note = obj.get("note")
        note = raw_note.strip() if isinstance(raw_note, str) and raw_note.strip() else None
        return DigestContent(summary=summary.strip(), key_points=points, note=note)

    @staticmethod
    def _format_error(label: str, exc: Exception) -> str:
        if isinstance(exc, requests.RequestException):
            response = getattr(exc, "response", None)
            if isinstance(response, requests.Response):
                body = response.text.strip()
                if body:
                    return f"{label}: HTTP {response.status_code} {body[:200]}"
                return f"{label}: HTTP {response.status_code}"
        return f"{label}: {exc}"


_BV_RE = re.compile(r"(BV[0-9A-Za-z]+)")
_DOUYIN_VIDEO_RE = re.compile(r"douyin\.com/video/(\d+)")
_SAFE_ID_RE = re.compile(r"[^0-9A-Za-z_-]")


_FILENAME_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')
_FILENAME_MAX_STEM = 60


def build_digest_filename(title: str, url: str) -> str:
    """报告文件名：优先用视频标题（清洗 Windows 非法字符、限长），无标题时退回平台+id。"""
    stem = _FILENAME_ILLEGAL_RE.sub(" ", str(title or ""))
    stem = re.sub(r"\s+", " ", stem).strip().strip(".")
    if not stem:
        bv = _BV_RE.search(url)
        if bv:
            return f"video_digest_bilibili_{bv.group(1)}.md"
        douyin = _DOUYIN_VIDEO_RE.search(url)
        if douyin:
            return f"video_digest_douyin_{douyin.group(1)}.md"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return f"video_digest_url_{digest[:16]}.md"
    return f"{stem[:_FILENAME_MAX_STEM].strip().strip('.')}.md"


_SOURCE_LABELS = {
    "bilibili_watchlater": "B站稍后看",
    "douyin_favorites": "抖音收藏",
}

_SUBTITLE_SOURCE_LABELS = {
    "bilibili_cc": "B站字幕",
    "videocaptioner_bijian": "语音转写",
    "glm_video": "AI 视频理解",
}


def _format_duration(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return "未知"
    minutes, sec = divmod(seconds, 60)
    return f"{minutes}分{sec:02d}秒"


def render_digest_markdown(
    *,
    title: str,
    url: str,
    source: str,
    owner: str | None,
    duration_seconds: int | None,
    subtitle_source: str,
    generated_at: str,
    digest: DigestContent,
) -> str:
    """渲染简报 Markdown：只用标题/无序列表/粗体/段落（md_render.py 支持子集）。"""
    note = digest.note or "无"

    points = "\n".join(f"- {point}" for point in digest.key_points)
    return (
        f"# {title}\n"
        "\n"
        f"- 来源：{_SOURCE_LABELS.get(source, source)}\n"
        f"- 链接：{url}\n"
        f"- 作者：{owner or '未知'}\n"
        f"- 时长：{_format_duration(duration_seconds)}\n"
        f"- 字幕来源：{_SUBTITLE_SOURCE_LABELS.get(subtitle_source, subtitle_source)}\n"
        f"- 生成时间：{generated_at}\n"
        "\n"
        "## 一句话总结\n"
        "\n"
        f"{digest.summary}\n"
        "\n"
        "## 核心要点\n"
        "\n"
        f"{points}\n"
        "\n"
        "## 备注\n"
        "\n"
        f"{note}\n"
    )


def write_digest_report(report_dir: Path, filename: str, content: str) -> Path:
    """原子落盘：先写 <filename>.tmp 再 os.replace，避免半截文件被渲染器读到。"""
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    final_path = report_dir / filename
    tmp_path = report_dir / f"{filename}.tmp"
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, final_path)
    return final_path

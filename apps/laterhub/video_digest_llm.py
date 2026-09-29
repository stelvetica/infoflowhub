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


# 字幕截断时写进 note 的固定提示（编排层截断字幕时传 truncated=True）
TRUNCATED_NOTE = "字幕因长度截断，总结仅基于前部分内容"

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

TRUNCATED_HINT = f"""
补充要求：字幕因长度被截断，你只能看到视频前部分内容。必须在 note 中注明"{TRUNCATED_NOTE}"。
"""

VIDEO_URL_HINT = """
补充要求：材料是视频本身（多模态输入），请直接观看视频内容后按要求总结。
"""


@dataclass(slots=True)
class DigestContent:
    summary: str
    key_points: list[str]
    note: str | None


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
        truncated: bool = False,
    ) -> DigestContent:
        system = SYSTEM_PROMPT + SUBTITLE_HINT + (TRUNCATED_HINT if truncated else "")
        user = (
            f"视频标题：{title}\n"
            f"作者：{owner or '未知'}\n\n"
            f"字幕文本：\n{subtitle_text}"
        )
        return self._with_fallback(system, user)

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

    def _generate(self, *, config: LLMConfig, system: str, user_content: Any) -> DigestContent:
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            # glm-5.3-flash 等思考模型把思考 token 计入 max_tokens 且不受 thinking 参数约束，
            # 上限过小会整条返回空 content（与 tagger.py 同一坑，故这里给到 3000）。
            "max_tokens": 3000,
        }
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
        return self._validate(self._parse_json_object(content))

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


def build_digest_filename(url: str) -> str:
    """报告文件名：B站取 BV 号、抖音取视频 id、其余用 url 哈希，保证只含 ASCII 安全字符。"""
    bv = _BV_RE.search(url)
    if bv:
        return f"video_digest_bilibili_{bv.group(1)}.md"
    douyin = _DOUYIN_VIDEO_RE.search(url)
    if douyin:
        return f"video_digest_douyin_{douyin.group(1)}.md"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    safe = _SAFE_ID_RE.sub("", digest)
    return f"video_digest_url_{safe}.md"


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
    truncated: bool = False,
) -> str:
    """渲染简报 Markdown：只用标题/无序列表/粗体/段落（md_render.py 支持子集）。"""
    if digest.note:
        note = digest.note
    elif truncated:
        note = TRUNCATED_NOTE
    else:
        note = "无"

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

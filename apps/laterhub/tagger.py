from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests import Response

from apps.laterhub.config import ENV_PATH
from apps.laterhub.feishu import load_project_env


DEFAULT_TAG = "娱乐-综艺/吃瓜"
TAG_OPTIONS = [
    "心理-认知",
    "心理-自我/情绪",
    "心理-他人/关系",
    "心理-职场",
    "技术-AI",
    "技术-效率/工具/开发",
    "技术-英语/写作",
    "技术-音乐/唱歌",
    "技术-摄影/剪辑",
    "技术-科学/科普",
    "社科-金融/商业/经济",
    "社科-历史",
    "社科-社会/时政",
    "人文-哲学/艺术",
    "人文-玄学",
    "生活-健康/健身",
    "生活-穿搭/保养",
    "生活-美食/旅行",
    "娱乐-影视/游戏/动漫",
    "娱乐-综艺/吃瓜",
    "解说-文学",
]

SYSTEM_PROMPT = """你是内容分类标签助手。你的任务是基于 source 和 title 给内容打标签。

你只能从下面 21 个标签中选择，禁止创造新标签，禁止改写标签文本：
- 心理-认知
- 心理-自我/情绪
- 心理-他人/关系
- 心理-职场
- 技术-AI
- 技术-效率/工具/开发
- 技术-英语/写作
- 技术-音乐/唱歌
- 技术-摄影/剪辑
- 技术-科学/科普
- 社科-金融/商业/经济
- 社科-历史
- 社科-社会/时政
- 人文-哲学/艺术
- 人文-玄学
- 生活-健康/健身
- 生活-穿搭/保养
- 生活-美食/旅行
- 娱乐-影视/游戏/动漫
- 娱乐-综艺/吃瓜
- 解说-文学

输出硬约束：
1. 只能输出 JSON 对象，格式必须是 {"tags": ["标签1", "标签2"]}。
2. tags 必须是数组。
3. 至少返回 1 个标签，最多返回 2 个标签。
4. 不要输出解释、理由、备注、Markdown、代码块。
5. 如果不确定，也必须从 21 个标签里选，不允许返回空数组。

判定顺序：
1. 先判断主主题，只选最核心的第一标签。
2. 只有当内容同时稳定覆盖第二个独立主题，且第二主题不是第一主题的细分改写时，才允许加第二标签。
3. 如果只是同一主题的不同表述、上下位概念、场景延伸，只保留 1 个最核心标签。

多标签门槛：
1. 单标签是默认，双标签是例外。
2. 只有标题明确同时包含两个并列主题时，才输出 2 个标签。
3. 不要因为“都沾一点”就打双标签。
4. 第二标签必须能脱离第一标签独立成立，否则不要加。

分类边界：
1. 心理-认知：认知偏差、思维模型、学习理解、决策判断。
2. 心理-自我/情绪：情绪管理、自我接纳、自尊、自我成长、内耗、焦虑、抑郁体验。
3. 心理-他人/关系：亲密关系、社交、人际边界、沟通、冲突、依恋。
4. 心理-职场：职场关系、职业发展、组织协作、向上管理、工作压力。
5. 技术-AI：AI 模型、智能体、提示词、AIGC、机器学习、AI 工具趋势。
6. 技术-效率/工具/开发：软件工具、工作流、编程开发、自动化、产品效率。
7. 技术-英语/写作：英语学习、表达、写作方法。
8. 技术-音乐/唱歌：乐理、演唱、发声、音乐技能。
9. 技术-摄影/剪辑：拍摄、镜头、后期、剪辑。
10. 技术-科学/科普：自然科学、前沿科学、通识科普。
11. 社科-金融/商业/经济：宏观经济、商业分析、投资、产业、公司经营。
12. 社科-历史：历史事件、人物、制度演变。
13. 社科-社会/时政：公共议题、社会现象、政策时政。
14. 人文-哲学/艺术：哲学、美学、艺术理论、审美表达。
15. 人文-玄学：命理、星座、塔罗、玄学解释框架。
16. 生活-健康/健身：营养、减脂、运动、养生、身体健康。
17. 生活-穿搭/保养：穿搭、护肤、保养、形象管理。
18. 生活-美食/旅行：做饭、探店、旅行、美食体验。
19. 娱乐-影视/游戏/动漫：影视作品、游戏、动漫内容本身。
20. 娱乐-综艺/吃瓜：明星、综艺、热点八卦、娱乐新闻。
21. 解说-文学：书籍、文学作品、作者、文本解读。

冲突约束：
1. “心理-认知 / 心理-自我/情绪 / 心理-他人/关系 / 心理-职场” 之间不要轻易双选，除非标题同时明确出现两个并列主题。
2. “技术-AI” 与 “技术-效率/工具/开发” 同时出现时：
   - 主题重心在 AI 本身，选 技术-AI。
   - 主题重心在工具流、编程、自动化落地，选 技术-效率/工具/开发。
   - 只有 AI 与开发/效率是明确并列主轴时，才双选。
3. “娱乐-影视/游戏/动漫” 与 “娱乐-综艺/吃瓜” 不要混用。
4. “社科-社会/时政” 与 “社科-金融/商业/经济” 不要因为宏观讨论就双选，除非经济与公共议题同样明确。

默认兜底：
1. 信息不足时，优先选最保守、最直接的主标签。
2. 明显是娱乐杂谈、热点围观、明星综艺、八卦，返回 {"tags": ["娱乐-综艺/吃瓜"]}。
3. 明显是作品内容、剧情、角色、游戏、动漫，优先返回 {"tags": ["娱乐-影视/游戏/动漫"]}。

下面是示例。你必须学习这些示例的判定方式，但不要复述示例。

正例 1：
输入：
{"source":"小红书","title":"总是反复想起自己说错话，怎么停止精神内耗？"}
输出：
{"tags":["心理-自我/情绪"]}

正例 2：
输入：
{"source":"公众号","title":"讨好型人格为什么总在关系里失去边界？"}
输出：
{"tags":["心理-他人/关系"]}

正例 3：
输入：
{"source":"YouTube","title":"认知偏差如何影响你的投资决策？"}
输出：
{"tags":["心理-认知","社科-金融/商业/经济"]}

正例 4：
输入：
{"source":"B站","title":"被领导反复否定后，职场人如何重建自信？"}
输出：
{"tags":["心理-职场","心理-自我/情绪"]}

正例 5：
输入：
{"source":"YouTube","title":"GPT Agent 实战：自动读取邮箱并生成日报"}
输出：
{"tags":["技术-AI","技术-效率/工具/开发"]}

正例 6：
输入：
{"source":"公众号","title":"2026 年 AI Agent 会如何改变软件开发流程？"}
输出：
{"tags":["技术-AI","技术-效率/工具/开发"]}

正例 7：
输入：
{"source":"B站","title":"番茄工作法为什么总失败？问题不在自律，在任务切分"}
输出：
{"tags":["技术-效率/工具/开发"]}

正例 8：
输入：
{"source":"播客","title":"美联储降息预期升温，会怎样影响美股与黄金？"}
输出：
{"tags":["社科-金融/商业/经济"]}

正例 9：
输入：
{"source":"公众号","title":"罗马共和国为什么会走向帝制？"}
输出：
{"tags":["社科-历史"]}

正例 10：
输入：
{"source":"微博","title":"某顶流恋情又反转了，吃瓜群众已经看麻了"}
输出：
{"tags":["娱乐-综艺/吃瓜"]}

正例 11：
输入：
{"source":"B站","title":"《进击的巨人》最终季到底想表达什么？"}
输出：
{"tags":["娱乐-影视/游戏/动漫"]}

正例 12：
输入：
{"source":"播客","title":"《局外人》里的荒诞感，为什么至今仍然击中现代人？"}
输出：
{"tags":["解说-文学"]}

反例 1：
输入：
{"source":"B站","title":"AI 写周报的 3 个提示词模板"}
错误输出：
{"tags":["技术-AI","技术-英语/写作"]}
正确输出：
{"tags":["技术-AI"]}
原因：主题重心是 AI 提示词使用，不是英语或通用写作方法。

反例 2：
输入：
{"source":"小红书","title":"为什么越懂事的人越容易委屈自己？"}
错误输出：
{"tags":["心理-认知","心理-自我/情绪"]}
正确输出：
{"tags":["心理-自我/情绪"]}
原因：主题核心是情绪与自我体验，不是认知模型讲解。

反例 3：
输入：
{"source":"公众号","title":"高敏感的人如何建立关系边界？"}
错误输出：
{"tags":["心理-自我/情绪","心理-他人/关系"]}
正确输出：
{"tags":["心理-他人/关系"]}
原因：虽然涉及自我感受，但主轴是关系边界。

反例 4：
输入：
{"source":"YouTube","title":"Cursor、Claude、Copilot 谁更适合程序员？"}
错误输出：
{"tags":["技术-AI","技术-效率/工具/开发","技术-英语/写作"]}
正确输出：
{"tags":["技术-AI","技术-效率/工具/开发"]}
原因：最多只能 2 个标签，且写作无关。

反例 5：
输入：
{"source":"微博","title":"新片票房扑街，主演回应上热搜"}
错误输出：
{"tags":["娱乐-影视/游戏/动漫"]}
正确输出：
{"tags":["娱乐-综艺/吃瓜"]}
原因：这是娱乐新闻与热点讨论，不是作品内容分析。

反例 6：
输入：
{"source":"B站","title":"《红楼梦》里贾宝玉的情感世界"}
错误输出：
{"tags":["娱乐-影视/游戏/动漫"]}
正确输出：
{"tags":["解说-文学"]}
原因：主语是文学作品解读，不是影视动漫作品。
"""


@dataclass(slots=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
        *,
        backup: bool = False,
        timeout: int = 20,
    ) -> "LLMConfig":
        target = Path(env_path) if env_path else ENV_PATH
        load_project_env(target)
        prefix = "BACKUP_" if backup else "PRIMARY_"
        base_url = os.getenv(f"{prefix}LLM_BASE_URL", "").strip().rstrip("/")
        api_key = os.getenv(f"{prefix}LLM_API_KEY", "").strip()
        model = os.getenv(f"{prefix}LLM_MODEL", "").strip()
        if not base_url or not api_key:
            raise ValueError(f"缺少 {prefix}LLM_BASE_URL 或 {prefix}LLM_API_KEY 配置")
        if not model:
            response = requests.get(
                f"{base_url}/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("data") or []
            if not items:
                raise RuntimeError("模型列表为空，无法自动选择 model")
            model = (items[0] or {}).get("id", "").strip()
        return cls(base_url=base_url, api_key=api_key, model=model)


class TaggerServiceUnavailable(RuntimeError):
    pass


class ContentTagger:
    def __init__(self, config: LLMConfig, backup_config: LLMConfig | None = None, timeout: int = 45) -> None:
        self.config = config
        self.backup_config = backup_config
        self.timeout = timeout
        self._disabled_reason: str | None = None

    def tag(self, *, title: str, source: str) -> str:
        if self._disabled_reason:
            raise TaggerServiceUnavailable(self._disabled_reason)
        try:
            return self._tag_with_config(self.config, title=title, source=source)
        except Exception as exc:
            if not self.backup_config:
                self._remember_service_failure(exc)
                raise
        try:
            return self._tag_with_config(self.backup_config, title=title, source=source)
        except Exception as exc:
            self._remember_service_failure(exc)
            raise

    def _tag_with_config(self, config: LLMConfig, *, title: str, source: str) -> str:
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"source: {source}\n"
                        f"title: {title}\n"
                        "请严格只返回 JSON 对象，例如 {\"tags\": [\"技术-AI\"]}"
                    ),
                },
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": 220,
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
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
        if content.startswith("{"):
            maybe_object = json.loads(content)
            if isinstance(maybe_object, dict):
                for key in ("tags", "labels", "result"):
                    value = maybe_object.get(key)
                    if isinstance(value, list):
                        tags = value
                        break
                else:
                    tags = self._parse_json_array(content)
            else:
                tags = self._parse_json_array(content)
        else:
            tags = self._parse_json_array(content)
        cleaned: list[str] = []
        for tag in tags:
            normalized = self._normalize_tag(str(tag).strip())
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        if not cleaned:
            raise RuntimeError(f"标签解析失败: {content}")
        return ",".join(cleaned[:2])

    def _remember_service_failure(self, exc: Exception) -> None:
        if not self._is_service_failure(exc):
            return
        self._disabled_reason = f"LLM 标签服务暂不可用: {exc}"

    @staticmethod
    def _is_service_failure(exc: Exception) -> bool:
        if isinstance(exc, requests.RequestException):
            response = getattr(exc, "response", None)
            if isinstance(response, Response) and response.status_code in {429, 502, 503, 504}:
                return True
            if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
                return True
        return False

    @staticmethod
    def _parse_json_array(text: str) -> list[Any]:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("tags", "labels", "result"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
        raise ValueError(f"不支持的标签返回格式: {text}")

    @staticmethod
    def _normalize_tag(text: str) -> str | None:
        candidates = [text]
        try:
            repaired = text.encode("latin1", "ignore").decode("utf-8", "ignore").strip()
            if repaired:
                candidates.append(repaired)
        except Exception:
            pass
        unique_candidates: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in unique_candidates:
                unique_candidates.append(candidate)
        for candidate in unique_candidates:
            if candidate in TAG_OPTIONS:
                return candidate
        normalized_candidates = [ContentTagger._tag_signature(candidate) for candidate in unique_candidates]
        for option in TAG_OPTIONS:
            option_signature = ContentTagger._tag_signature(option)
            for candidate_signature in normalized_candidates:
                if not candidate_signature:
                    continue
                if candidate_signature == option_signature:
                    return option
                if candidate_signature in option_signature or option_signature in candidate_signature:
                    return option
        return None

    @staticmethod
    def _tag_signature(text: str) -> str:
        return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")

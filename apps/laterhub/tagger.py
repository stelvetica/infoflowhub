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


# 标签体系唯一真源：顺序即展示顺序。TAG_OPTIONS / 白名单 / 提示词清单全部由它派生，
# 任何地方不得再手抄一份标签清单。
TAG_CATALOG: list[tuple[str, str]] = [
    ("心理_认知/学习", "认知科学、思维机制、学习方法、记忆与决策偏差；不收哲学论证、命理占卜或纯职场经验。"),
    ("心理_自我/情绪", "自我理解、情绪识别调节、内在成长与心理状态；不收关系互动或职业发展。"),
    ("心理_他人/关系", "沟通、亲密关系、社交、人际互动与他人心理；不收单纯自我疗愈或职业技能。"),
    ("心理_职场", "职业选择、面试、管理、工作协作与职场心理；标题须明确工作、公司、面试、管理或职业场景。"),
    ("技术_AI", "AI、机器学习、大模型、提示词、智能体等作为主题本身；AI 只是工具或实现手段时不选此标签。"),
    ("技术_英语", "英语学习、口语、发音、词汇、语法、雅思、托福、四六级；不收一般表达、写作或读书方法。"),
    ("技术_摄影", "拍摄、镜头、构图、灯光、修图、后期、剪辑等视觉影像实操；不收艺术理论或影视评论。"),
    ("技术_唱歌", "声乐训练、唱歌技巧、发声、气息、混声、音准与演唱教学；不收音乐欣赏或普通翻唱展示。"),
    ("技术_写作", "帮助自己写得更好的表达、写作、文案、文章结构与读书方法；不收小说、网文、名著故事解说。"),
    ("技术_工具", "软件、工作流、效率方法、资料管理与自动化落地；不收编程实现。AI 是主题本身时优先技术_AI。"),
    ("技术_开发", "编程、代码、数据库、架构、编程语言、程序员与软件开发；普通软件使用和工作流归技术_工具。"),
    ("社科_科学/科普", "自然科学、科学知识与大众科学解释；AI 原理、模型或大模型行业作为主题时归技术_AI。"),
    ("社科_历史", "历史事件、人物、时代、文明与历史考据；不收虚构作品剧情讲解。"),
    ("社科_社会", "社会现象、公共议题、政治时事、群体与制度观察；经济金融专业内容归社科_经济/金融。"),
    ("社科_经济/金融", "经济学、金融、商业、消费、投资与市场；不收一般职场或购物攻略。"),
    ("社科_哲学", "哲学家、哲学流派、哲学论证、思想史与抽象世界观；不收命理占卜、灵性修心或认知科学。"),
    ("社科_艺术", "艺术史、艺术理论、审美与作品分析；摄影器材、构图、后期等实操归技术_摄影。"),
    ("社科_音乐", "音乐欣赏、歌曲、音乐史、音乐评论、翻唱与音乐文化；发声、气息、混声教学归技术_唱歌。"),
    ("社科_玄学", "命理、占卜、风水、星座、宗教灵性与修心；不收哲学论证或认知科学。"),
    ("生活_健康", "养生、营养、饮食健康、睡眠、疾病预防与一般身体健康知识；动作训练、健身计划和塑形归生活_健身。"),
    ("生活_健身", "运动、锻炼、训练、体态纠正、塑形、减脂动作与健身计划；单纯营养、睡眠或疾病知识归生活_健康。"),
    ("生活_穿搭", "穿衣、搭配、选衣、风格、场合着装与配饰；不收护肤、化妆、发型或泛泛变美。"),
    ("生活_保养", "护肤、化妆、发型、护发、变美与外表护理；明确服装搭配归生活_穿搭。"),
    ("生活_美食", "做饭、餐馆、菜品、吃什么与美食体验；标题核心是吃店和菜品时，即使在旅行中也归此类。"),
    ("生活_旅行", "旅行目的地、路线、住宿、景点与出行规划体验；单纯餐馆或菜品测评归生活_美食。"),
    ("娱乐_影视/游戏/动漫/解说", "影视、电视剧、电影、游戏、动漫及其剧情解说、盘点和评论；文学原作故事归娱乐_文学/解说。"),
    ("娱乐_文学/解说", "小说、网文、名著及文学作品的剧情、人物、章节和故事解说；写作技巧归技术_写作。"),
    ("娱乐_综艺", "综艺、脱口秀、访谈、真人秀及节目内容本身；明星恋情、八卦、热搜和娱乐新闻归娱乐_吃瓜。"),
    ("娱乐_吃瓜", "明星八卦、恋情、争议、热搜、娱乐新闻与行业 gossip；节目内容本身归娱乐_综艺。"),
    ("其他_无法判断", "仅凭 source 和 title 无法可靠判断，或标题垃圾、过短、无语义、歧义过高；禁止猜测。"),
]

TAG_OPTIONS = [name for name, _ in TAG_CATALOG]
TAG_WHITELIST = frozenset(TAG_OPTIONS)
DEFAULT_TAG = "其他_无法判断"


_SYSTEM_PROMPT_TEMPLATE = """你是稍后处理队列的标题分类器。你只能依据输入的 source 和 title 判断主题，不得假设正文、账号背景或未提供的信息。

【唯一标签清单】
只能从下列标签中选择，必须逐字复制；禁止创造、翻译、缩写、改写、拆分、合并标签：
__TAG_LIST__

【标签定义】
__TAG_DEFS__

【判定顺序】
1. 先判断标题是否有足够直接语义。标题是垃圾、过短、无语义、只有人名日期或纯感叹句时，直接输出「其他_无法判断」。
2. 核心主题优先：AI 本身→技术_AI；写代码/数据库/架构→技术_开发；软件工具/工作流/效率→技术_工具；声乐训练→技术_唱歌；影像实操→技术_摄影；自然科学→社科_科学/科普。
3. 「自己怎么做得更好」是技术类技能（技术_英语/写作/摄影/唱歌）；「别人讲作品故事」是娱乐类（娱乐_文学/解说、娱乐_影视/游戏/动漫/解说）。
4. 节目内容本身→娱乐_综艺；明星恋情/八卦/热搜/娱乐新闻→娱乐_吃瓜。
5. 只在两个主题都被标题直接证明、且各自独立时输出两个标签；否则只输出一个。
6. 以上规则无法可靠裁决时输出「其他_无法判断」，不得猜测。

【硬约束】
1. 只能输出 JSON 对象，形状必须是 {"tags":["标签"]}。
2. tags 必须是数组，至少 1 个、最多 2 个标签。
3. 每个标签必须逐字来自唯一标签清单，禁止输出清单外的任何字符串。
4. 不要输出解释、理由、备注、Markdown、代码块或额外字段。
5. 无法判断时必须输出 {"tags":["其他_无法判断"]}，禁止猜测，禁止返回空数组。

【示例】
正例1 输入 {"source":"douyin_favorite","title":"English Booster：CET/IELTS高频词"} → 输出 {"tags":["技术_英语"]}
正例2 输入 {"source":"manual_verify","title":"刘辉读书：如何提高表达能力？#表达技巧"} → 输出 {"tags":["技术_写作"]}
正例3 输入 {"source":"douyin_favorite","title":"小白debug：我用TRAE写了个摸鱼软件 #程序员 #java #python"} → 输出 {"tags":["技术_开发"]}
正例4 输入 {"source":"douyin_favorite","title":"K教摄影：摄影新手最常犯的几个构图禁忌 #摄影教程"} → 输出 {"tags":["技术_摄影"]}
正例5 输入 {"source":"douyin_favorite","title":"杰森带你唱歌：混声是什么？一个视频教会你混声是什么！#声乐技巧"} → 输出 {"tags":["技术_唱歌"]}
正例6 输入 {"source":"douyin_favorite","title":"一条大肥鹅：寒假极速吃广州！两天吃了26家 #广州美食"} → 输出 {"tags":["生活_美食"]}
正例7 输入 {"source":"douyin_favorite","title":"刘春歧：什么样的喜剧，演完还需要马东老师道歉？#喜人 #脱口秀"} → 输出 {"tags":["娱乐_综艺"]}
反例1 输入 {"source":"douyin_favorite","title":"基本盘成员于20211009发布的作品"}
错误输出 {"tags":["娱乐_吃瓜"]}　正确输出 {"tags":["其他_无法判断"]}
原因：标题只有上传模板，没有可确认主题，不能猜测。
反例2 输入 {"source":"manual_verify","title":"新片票房扑街，主演回应上热搜"}
错误输出 {"tags":["娱乐_影视/游戏/动漫/解说"]}　正确输出 {"tags":["娱乐_吃瓜"]}
原因：核心是明星与热搜新闻，不是作品内容。
反例3 输入 {"source":"douyin_favorite","title":"周小辣：《茶花女》名著解读 #深夜读书"}
错误输出 {"tags":["技术_写作"]}　正确输出 {"tags":["娱乐_文学/解说"]}
原因：这是讲文学作品故事，不是教你写作。
"""


def _build_system_prompt(catalog: list[tuple[str, str]]) -> str:
    """由 TAG_CATALOG 注入标签清单与定义行，杜绝提示词里出现第二份手写清单。"""
    tag_list = "\n".join(name for name, _ in catalog)
    tag_defs = "\n".join(f"{name}：{definition}" for name, definition in catalog)
    return _SYSTEM_PROMPT_TEMPLATE.replace("__TAG_LIST__", tag_list).replace("__TAG_DEFS__", tag_defs)


SYSTEM_PROMPT = _build_system_prompt(TAG_CATALOG)


def normalize_tag(text: str | None) -> str | None:
    """严格白名单归一化：仅裁剪空白 + latin1/utf-8 编码修复，命中白名单才返回规范名。

    刻意不做近似/签名包含匹配——旧实现会把「人文-哲学」这类已废弃标签吞进某个合法
    标签（如 人文-哲学/艺术），未知串一律 None，由调用方兜底。
    """
    raw = ("" if text is None else str(text)).strip()
    candidates = [raw]
    try:
        repaired = raw.encode("latin1", "ignore").decode("utf-8", "ignore").strip()
    except Exception:  # noqa: BLE001
        repaired = ""
    if repaired and repaired not in candidates:
        candidates.append(repaired)
    for candidate in candidates:
        if candidate in TAG_WHITELIST:
            return candidate
    return None


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


class TaggerTemporaryError(RuntimeError):
    pass


class TaggerPermanentError(RuntimeError):
    pass


class ContentTagger:
    def __init__(self, config: LLMConfig, backup_config: LLMConfig | None = None, timeout: int = 45) -> None:
        self.config = config
        self.backup_config = backup_config
        self.timeout = timeout

    def tag(self, *, title: str, source: str) -> str:
        """依次尝试主/备 provider，任一家成功即返回。

        备用是兜底而非"只对服务故障才用"：主家 4xx 拒绝、响应畸形、content 为空都可能只是
        这一家/这一次的问题，换一家常常就成（实测同一标题 primary 报 HTTP 400 的同一刻，
        backup 直接返回合法标签）。故这里只把"这家自身坏了、换家也没用"的错判为永久。
        """
        attempts: list[tuple[str, LLMConfig]] = [("primary", self.config)]
        if self.backup_config:
            attempts.append(("backup", self.backup_config))
        transient_errors: list[str] = []
        permanent_errors: list[str] = []
        for label, config in attempts:
            try:
                return self._tag_with_config(config, title=title, source=source)
            except Exception as exc:  # noqa: BLE001
                formatted = self._format_provider_error(label, exc)
                if self._is_permanent_failure(exc):
                    permanent_errors.append(formatted)
                else:
                    transient_errors.append(formatted)
        # 两家都失败：只要有一家是"可重试"的错就留待下轮统一补标签，不判死
        if transient_errors:
            raise TaggerTemporaryError("; ".join(transient_errors))
        raise TaggerPermanentError("; ".join(permanent_errors))

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
                        "请严格只返回 JSON 对象，例如 {\"tags\": [\"技术_AI\"]}"
                    ),
                },
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            # glm-5.3-flash 等思考模型把思考 token 计入 max_tokens 且不受 thinking 参数约束，
            # 上限过小会整条返回空 content（打标签直接失败）
            "max_tokens": 1500,
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
        # content 可能是 null（思考模型把 max_tokens 全用在思考上时实测如此）：必须给出可读
        # 错误，不能让它退化成 'NoneType' object has no attribute 'strip' 这种假象。
        content = str(message.get("content") or "").strip()
        if not content:
            raise RuntimeError("模型返回空 content（思考模型可能耗尽 max_tokens）")
        tags = self._parse_json_array(content)
        cleaned: list[str] = []
        for tag in tags:
            normalized = normalize_tag(str(tag))
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        if not cleaned:
            # 模型没给出任何一个合法标签（空数组、非字符串项、已废弃标签都算）：这是这一次
            # 响应的失败，不是"这条内容无法判断"。必须抛错，才能让 tag() 去试备用 provider、
            # 并让该行留待下轮重试——直接写 DEFAULT_TAG 会把 provider 的静默劣化固化成
            # 一个合法标签，主备降级与补偿机制随之失效。
            raise RuntimeError(f"模型未返回任何合法标签: {content[:200]}")
        return ",".join(cleaned[:2])

    @staticmethod
    def _is_permanent_failure(exc: Exception) -> bool:
        """这家 provider 自身坏了、换家也没用的错 → 本轮不再重试（下轮仍会统一补标签）。

        只认 401/403（鉴权）与 404（端点或模型不存在）：配置类错误，重试无意义。
        其余一律归"可重试"——包括 400 与响应畸形：实测同一时刻换家就能出标签，正是备用该
        兜的场景；把它们判死会让备用永远不上场（本次事故：400 与 content 为空都直接 break）。
        ponytail: 无法可靠区分"400 里说模型不存在"与其它 400，故只按状态码判；模型名配错会
        在每轮以可见文案重复暴露，不会静默。
        """
        if isinstance(exc, requests.RequestException):
            response = getattr(exc, "response", None)
            if isinstance(response, Response) and response.status_code in {401, 403, 404}:
                return True
        return False

    @staticmethod
    def _format_provider_error(label: str, exc: Exception) -> str:
        if isinstance(exc, requests.RequestException):
            response = getattr(exc, "response", None)
            if isinstance(response, Response):
                body = response.text.strip()
                if body:
                    return f"{label}: HTTP {response.status_code} {body[:200]}"
                return f"{label}: HTTP {response.status_code}"
        return f"{label}: {exc}"

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

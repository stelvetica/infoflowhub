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
    ("心理-认知", "认知偏差、思维模型、学习理解、决策判断、认知能力"),
    ("心理-自我/情绪", "情绪管理、自我接纳、自尊、自我成长、内耗、焦虑"),
    ("心理-他人/关系", "亲密关系、婚恋、择偶、社交、沟通、边界、冲突、依恋"),
    ("心理-职场", "求职、职业发展、组织协作、领导下属、销售、工作场景压力"),
    ("技术-AI", "AI 模型、智能体、提示词、AIGC、机器学习、AI 产品与行业趋势"),
    ("技术-效率/工具/开发", "软件工具、工作流、编程、自动化、数据库、产品效率"),
    ("技术-创作/表达", "英语、写作、表达、唱歌、摄影、拍摄、剪辑等创作实操技能"),
    ("技术-科学/科普", "数学、物理、化学、生物、自然科学与形式科学科普"),
    ("社科-金融/商业/消费", "投资、金融、宏观经济、公司经营、营销、电商、消费决策与省钱"),
    ("社科-历史", "历史事件、人物、时代、战争、制度演变"),
    ("社科-社会/时政", "公共政策、社会现象、教育、政治、国际局势、公共议题"),
    ("文化-哲学/文学/艺术/音乐", "哲学、文学/网文/名著解读、艺术理论、审美、音乐欣赏、翻唱与歌曲推荐"),
    ("文化-玄学", "命理、占卜、星座、塔罗、易经、道家玄学与修心解释"),
    ("生活-健康/健身", "运动、健身、营养、减脂、体态、养生、身体健康"),
    ("生活-穿搭/保养", "穿搭、护肤、化妆、变美、发型、形象管理"),
    ("生活-美食/旅行", "做饭、菜谱、探店、美食体验、旅游路线与旅行体验"),
    ("娱乐-影视/游戏/动漫", "电影、剧集、游戏、动漫及其剧情、角色、评价、盘点、精讲、解说"),
    ("娱乐-综艺/吃瓜", "综艺、脱口秀、喜剧节目、明星、娱乐新闻、恋情、八卦、热搜"),
    ("其他-无法判断", "仅凭 source/title 无法确认稳定主题"),
]

TAG_OPTIONS = [name for name, _ in TAG_CATALOG]
TAG_WHITELIST = frozenset(TAG_OPTIONS)
DEFAULT_TAG = "其他-无法判断"


_SYSTEM_PROMPT_TEMPLATE = """你是 laterhub 内容标签助手。你只能依据输入的 source 和 title 判断主题，看不到正文；source 只是弱线索，不能弥补没有主题信息的 title。

【唯一标签清单】
只能从下列标签中选择，必须逐字复制；禁止创造、翻译、缩写、改写、拆分、合并标签：
__TAG_LIST__

【标签定义】
__TAG_DEFS__

【硬约束】
1. 只能输出 JSON 对象，形状必须是 {"tags":["标签"]}。
2. tags 必须是数组，至少 1 个、最多 2 个标签。
3. 每个标签必须逐字来自唯一标签清单，禁止输出清单外的任何字符串。
4. 不要输出解释、理由、备注、Markdown、代码块或额外字段。
5. 无法判断时必须输出 {"tags":["其他-无法判断"]}，禁止猜测，禁止返回空数组。

【判定顺序】
1. 先看 title 是否有稳定主题；只有作者名、日期、上传模板、口号、感叹句而无主题时，直接选「其他-无法判断」。
2. 按标题中明确的主题词选主标签；忽略「解说、精讲、漫剪、盘点、一口气看完」等**内容形式词**，形式不产生标签。
3. 具体影视/游戏/动漫作品的剧情、角色、评价、解读归「娱乐-影视/游戏/动漫」；综艺、脱口秀、明星、热搜、八卦归「娱乐-综艺/吃瓜」；文学/网文/名著阅读解读归「文化-哲学/文学/艺术/音乐」。
4. 选择最能说明「用户会因为什么主题而消费这条内容」的标签；source 不能单独决定标签。
5. 只有标题明确包含两个独立并列主题时，才考虑第二标签。

【多标签门槛】
1. 单标签是默认，双标签是例外。
2. 两个主题必须各有明确的标题证据，且各自都能独立成为筛选理由。
3. 同一主题的上下位词、场景、例子、结果、方法、形式词都不构成第二标签。
4. 不要因为「都沾一点」而双选。

【冲突约束】
1. 心理四类按标题主问题选：认知模型→心理-认知；情绪体验→心理-自我/情绪；关系互动→心理-他人/关系；工作场景→心理-职场。
2. AI 是主题本身选「技术-AI」；AI 只是编程/自动化/工具的实现手段选「技术-效率/工具/开发」；两者都明确并列才双选。
3. 「技术-创作/表达」是实操技能；「文化-哲学/文学/艺术/音乐」是思想、作品与音乐欣赏。不要因为出现「唱歌/摄影/写作」字样就双选。
4. 具体作品解析与明星综艺八卦分属两个娱乐标签，不混用。
5. 「社科-金融/商业/消费」与「社科-社会/时政」仅在两个主权轴都明确时双选；历史事件不因涉及政治而双选。

【兜底】
1. 信息不足、主题互相冲突无法裁决、或只有来源没有主题时，输出「其他-无法判断」。
2. 已确认一个主题、但补充内容不足以构成第二独立主题时，只返回该主题。
3. 不得用娱乐标签接收垃圾标题，不得用「最接近的标签」代替不确定判断。

【示例】
正例1 输入 {"source":"douyin_favorite","title":"杨哥是我：面墙蹲 #体态纠正"} → 输出 {"tags":["生活-健康/健身"]}
正例2 输入 {"source":"bilibili_watchlater","title":"GPT Agent 实战：自动读取邮箱并生成日报"} → 输出 {"tags":["技术-AI","技术-效率/工具/开发"]}
正例3 输入 {"source":"douyin_favorite","title":"新生电影：2026最新电影高清精讲《团战之夜》"} → 输出 {"tags":["娱乐-影视/游戏/动漫"]}
正例4 输入 {"source":"manual_verify","title":"周小辣：《茶花女》名著解读"} → 输出 {"tags":["文化-哲学/文学/艺术/音乐"]}
正例5 输入 {"source":"manual_verify","title":"美联储降息预期升温，会怎样影响美股与黄金？"} → 输出 {"tags":["社科-金融/商业/消费"]}

反例1 输入 {"source":"douyin_favorite","title":"基本盘成员于20211009发布的作品"}
错误输出 {"tags":["娱乐-综艺/吃瓜"]}　正确输出 {"tags":["其他-无法判断"]}
原因：标题只有上传模板，没有可确认主题，不能猜测娱乐。

反例2 输入 {"source":"manual_verify","title":"新片票房扑街，主演回应上热搜"}
错误输出 {"tags":["娱乐-影视/游戏/动漫"]}　正确输出 {"tags":["娱乐-综艺/吃瓜"]}
原因：核心是明星与热搜新闻，不是作品内容。

反例3 输入 {"source":"bilibili_watchlater","title":"AI 写周报的 3 个提示词模板"}
错误输出 {"tags":["技术-AI","技术-创作/表达"]}　正确输出 {"tags":["技术-AI"]}
原因：写周报只是 AI 的应用场景，不是表达技能主轴。
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
                        "请严格只返回 JSON 对象，例如 {\"tags\": [\"技术-AI\"]}"
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

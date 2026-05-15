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


DEFAULT_TAG = "??-??/??"
TAG_OPTIONS = [
    "??-??",
    "??-??/??",
    "??-??/??",
    "??-??",
    "??-AI",
    "??-??/??/??",
    "??-??/??",
    "??-??/??",
    "??-??/??",
    "??-??/??",
    "??-??/??/??",
    "??-??",
    "??-??/??",
    "??-??/??",
    "??-??",
    "??-??/??",
    "??-??/??",
    "??-??/??",
    "??-??/??/??",
    "??-??/??",
    "??-??",
]

SYSTEM_PROMPT = """???????????????? 21 ???????????????
??-??
??-??/??
??-??/??
??-??
??-AI
??-??/??/??
??-??/??
??-??/??
??-??/??
??-??/??
??-??/??/??
??-??
??-??/??
??-??/??
??-??
??-??/??
??-??/??
??-??/??
??-??/??/??
??-??/??
??-??

???
1. ???? 2 ???????
2. ???????????????? ["??-??/??"]?
3. ??? JSON ?????????? Markdown?"""


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
            raise ValueError(f"?? {prefix}LLM_BASE_URL ? {prefix}LLM_API_KEY ??")
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
                raise RuntimeError("????????????? model")
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
                        f"??: {source}\n"
                        f"??: {title}\n"
                        "????? JSON ????? [\"??-AI\"]"
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": 40,
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
        tags = self._parse_json_array(content)
        cleaned: list[str] = []
        for tag in tags:
            normalized = self._normalize_tag(str(tag).strip())
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        if not cleaned:
            raise RuntimeError(f"??????: {content}")
        return "?".join(cleaned[:2])

    def _remember_service_failure(self, exc: Exception) -> None:
        if not self._is_service_failure(exc):
            return
        self._disabled_reason = f"LLM ???????????????: {exc}"

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
            return json.loads(text)
        except json.JSONDecodeError:
            return ast.literal_eval(text)

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

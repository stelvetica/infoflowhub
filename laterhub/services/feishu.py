from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from laterhub.services.config import ENV_PATH

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def load_project_env(env_path: str | Path | None = None) -> None:
    if load_dotenv is None:
        return
    target = Path(env_path) if env_path else ENV_PATH
    if target.exists():
        load_dotenv(dotenv_path=target, override=False)


@dataclass(slots=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    bitable_app_token: str
    bitable_table_id: str

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "FeishuConfig":
        load_project_env(env_path)
        mapping = {
            "app_id": "FEISHU_APP_ID",
            "app_secret": "FEISHU_APP_SECRET",
            "bitable_app_token": "FEISHU_BITABLE_APP_TOKEN",
            "bitable_table_id": "FEISHU_BITABLE_TABLE_ID",
        }
        values: dict[str, str] = {}
        missing: list[str] = []
        for field_name, env_name in mapping.items():
            value = os.getenv(env_name, "").strip()
            if not value:
                missing.append(env_name)
            values[field_name] = value
        if missing:
            raise ValueError("缺少飞书环境变量: " + ", ".join(missing))
        return cls(**values)


class FeishuBitableClient:
    def __init__(self, config: FeishuConfig, timeout: int = 20) -> None:
        self.config = config
        self.timeout = timeout
        self.base_url = "https://open.feishu.cn/open-apis"
        self._tenant_access_token: str | None = None

    def get_tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        response = requests.post(
            f"{self.base_url}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.config.app_id,
                "app_secret": self.config.app_secret,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError("飞书返回成功，但 tenant_access_token 为空")
        self._tenant_access_token = token
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_tenant_access_token()}",
            "Content-Type": "application/json",
        }

    def list_records(
        self,
        *,
        page_token: str | None = None,
        page_size: int = 100,
        filter_expr: str | None = None,
    ) -> dict[str, Any]:
        url = (
            f"{self.base_url}/bitable/v1/apps/{self.config.bitable_app_token}"
            f"/tables/{self.config.bitable_table_id}/records"
        )
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        if filter_expr:
            params["filter"] = filter_expr
        response = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        data = response.json()
        if response.status_code >= 400 or data.get("code") != 0:
            raise RuntimeError(f"飞书读取记录失败: {data}")
        return data

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        url = (
            f"{self.base_url}/bitable/v1/apps/{self.config.bitable_app_token}"
            f"/tables/{self.config.bitable_table_id}/records"
        )
        response = requests.post(
            url,
            headers=self._headers(),
            json={"fields": fields},
            timeout=self.timeout,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw_text": response.text}
        if response.status_code >= 400:
            raise RuntimeError(f"飞书写入记录失败: HTTP {response.status_code} {data}")
        if data.get("code") != 0:
            raise RuntimeError(f"飞书写入记录失败: {data}")
        return data

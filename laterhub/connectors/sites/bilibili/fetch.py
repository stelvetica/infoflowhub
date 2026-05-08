from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
BILIBILI_API_CANDIDATES = [
    "https://api.bilibili.com/x/v2/history/toview/web",
    "https://api.bilibili.com/x/v2/history/toview",
]


def load_project_env(env_path: str | Path | None = None) -> None:
    if load_dotenv is None:
        return
    target = Path(env_path) if env_path else DEFAULT_ENV_PATH
    if target.exists():
        load_dotenv(dotenv_path=target, override=False)


@dataclass(slots=True)
class BilibiliConfig:
    cookie: str
    ps: int = 20

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "BilibiliConfig":
        load_project_env(env_path)
        cookie = os.getenv("BILIBILI_COOKIE", "").strip()
        sessdata = os.getenv("BILIBILI_SESSDATA", "").strip()
        bili_jct = os.getenv("BILIBILI_BILI_JCT", "").strip()
        dedeuserid = os.getenv("BILIBILI_DEDEUSERID", "").strip()

        if not cookie and sessdata:
            pieces = [f"SESSDATA={sessdata}"]
            if bili_jct:
                pieces.append(f"bili_jct={bili_jct}")
            if dedeuserid:
                pieces.append(f"DedeUserID={dedeuserid}")
            cookie = "; ".join(pieces)

        if not cookie:
            raise ValueError("缺少 B 站登录态。请在 .env 中填写 BILIBILI_COOKIE，或至少填写 BILIBILI_SESSDATA。")

        return cls(cookie=cookie)


class BilibiliWatchLaterFetcher:
    def __init__(self, config: BilibiliConfig, timeout: int = 20) -> None:
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/watchlater/list",
                "Cookie": self.config.cookie,
            }
        )

    def fetch(self) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for url in BILIBILI_API_CANDIDATES:
            try:
                response = self.session.get(url, params={"ps": self.config.ps}, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"B 站接口返回异常: {data}")
                items = self._extract_items(data)
                if items is None:
                    raise RuntimeError(f"无法识别 B 站稍后看返回结构: {data}")
                return items
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"获取 B 站稍后看失败: {last_error}")

    @staticmethod
    def _extract_items(data: dict[str, Any]) -> list[dict[str, Any]] | None:
        container = data.get("data")
        if isinstance(container, dict):
            for key in ("list", "items"):
                if isinstance(container.get(key), list):
                    return [BilibiliWatchLaterFetcher._normalize_item(x) for x in container[key]]
            if isinstance(container.get("list"), dict) and isinstance(container["list"].get("list"), list):
                return [BilibiliWatchLaterFetcher._normalize_item(x) for x in container["list"]["list"]]
        if isinstance(container, list):
            return [BilibiliWatchLaterFetcher._normalize_item(x) for x in container]
        return None

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
        bvid = item.get("bvid") or item.get("bv_id") or ""
        aid = item.get("aid") or item.get("id")
        title = item.get("title") or item.get("page_title") or "未命名视频"
        if bvid:
            url = f"https://www.bilibili.com/video/{bvid}"
        elif aid:
            url = f"https://www.bilibili.com/video/av{aid}"
        else:
            url = item.get("short_link_v2") or item.get("uri") or ""
        owner = item.get("owner") or {}
        owner_name = owner.get("name") or item.get("author") or ""
        return {
            "url": url,
            "title": title,
            "source": "bilibili_watchlater",
            "owner": owner_name,
            "tags": None,
            "raw": item,
        }


def fetch_bilibili_watchlater(env_path: str | Path | None = None) -> list[dict[str, Any]]:
    config = BilibiliConfig.from_env(env_path)
    return BilibiliWatchLaterFetcher(config).fetch()

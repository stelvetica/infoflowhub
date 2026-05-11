from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = BASE_DIR / "config"
SOURCES_PATH = CONFIG_DIR / "rss_sources.json"
SETTINGS_PATH = CONFIG_DIR / "rss_settings.json"


def load_sources() -> List[Dict[str, Any]]:
    data = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    return data.get("sources", [])


def save_sources(sources: List[Dict[str, Any]]) -> None:
    SOURCES_PATH.write_text(
        json.dumps({"sources": sources}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {
            "rsshub": {
                "public_base": "https://rsshub.app",
                "self_hosted_base": "",
                "prefer_self_hosted": False,
            }
        }
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))

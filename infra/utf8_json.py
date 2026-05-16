from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json_utf8(path: Path, *, default: Any | None = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        if default is not None:
            return default
        raise


def dump_json_utf8(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")

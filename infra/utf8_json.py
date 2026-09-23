from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from infra.text_normalizer import normalize_utf8_obj


def load_json_utf8(path: Path, *, default: Any | None = None) -> Any:
    try:
        return normalize_utf8_obj(json.loads(path.read_text(encoding="utf-8-sig")))
    except Exception:
        if default is not None:
            return default
        raise


def dump_json_utf8(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalize_utf8_obj(value), ensure_ascii=False, indent=2) + "\n"
    # 同目录临时文件 + os.replace：Windows 上原子替换，避免直接 truncate 后进程被杀
    # 留下半截 JSON（读取端对损坏 JSON 静默回退默认值，会丢 last_run_date 触发整日重跑）。
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise

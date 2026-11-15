"""夜间批量解析：22:00 起跑（由计划任务拉起），先 3 条小样再跑心理/社科两批。

为什么用独立脚本而不是直接排 CLI：需要按顺序跑、批次间随机停顿、结束后把 LLM 切回 flash。
日志固定写 runtime/tmp/night_YYYYMMDD.log（UTF-8）。
"""

from __future__ import annotations

import datetime as dt
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\ETT\Playground\infoflowhub")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "runtime" / "tmp" / f"night_{dt.date.today():%Y%m%d}.log"

PSY_TAGS = "心理_认知/学习方法,心理_自我/情绪分析,心理_他人/关系,心理_职场"
SOCIAL_TAGS = "社科_历史,社科_哲学/社会/经济/金融"


def log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def run_digest(label: str, tag: str, limit: int, extra: list[str] | None = None) -> int:
    args = [
        str(PY),
        "-m",
        "apps.laterhub.video_digest",
        "--source",
        "douyin_favorite",
        "--tag",
        tag,
        "--limit",
        str(limit),
        "--keep-temp",
        *(extra or []),
    ]
    log(f"[{label}] start: limit={limit} tag={tag} extra={extra or []}")
    started = time.time()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with LOG.open("a", encoding="utf-8") as handle:
        code = subprocess.run(args, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env).returncode
    log(f"[{label}] end: exit={code} elapsed={int(time.time() - started)}s")
    return code


def restore_flash() -> None:
    """把主 LLM 切回 group/flash（跑批期间临时用 Pro）。"""
    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        changed = False
        for index, line in enumerate(lines):
            if line.startswith("PRIMARY_LLM_MODEL="):
                lines[index] = "PRIMARY_LLM_MODEL=group/flash"
                changed = True
        if changed:
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"[llm] restored PRIMARY_LLM_MODEL=group/flash (changed={changed})")
    except OSError as exc:
        log(f"[llm] restore failed: {exc}")


def main() -> int:
    log("======== night run start ========")
    run_digest("probe", PSY_TAGS, 3, extra=["--retry-failed"])
    time.sleep(120)
    run_digest("psychology", PSY_TAGS, 300, extra=["--retry-failed"])
    pause = random.randint(120, 300)
    log(f"[pause] {pause}s before social batch")
    time.sleep(pause)
    run_digest("social", SOCIAL_TAGS, 100)
    restore_flash()
    log("======== night run done ========")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""第二夜批次：等第一批（心理+社科）跑完后自动接力，只跑「知识变化慢」的领域。

K1 稳定知识：科普 / 东哲 / 艺术·音乐 / 文学·艺术 / 英语 / 写作 / 摄影 / 唱歌
K2 生活技能：健身 / 健康保养 / 美食 / 穿搭 / 旅行
排除：AI·工具·开发（迭代太快）、影视·游戏·动漫、综艺·吃瓜、无法判断。

由 Windows 计划任务 23:00 拉起；先轮询等第一批写出 [night run done]，再依次跑。
"""

from __future__ import annotations

import datetime as dt
import os
import random
import subprocess
import time
from pathlib import Path

ROOT = Path(r"C:\Users\ETT\Playground\infoflowhub")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
STAGE1_LOG = ROOT / "runtime" / "tmp" / f"night_{dt.date.today():%Y%m%d}.log"
LOG = ROOT / "runtime" / "tmp" / f"night_stage2_{dt.date.today():%Y%m%d}.log"

K1_TAGS = ",".join(
    [
        "社科_科学/科普",
        "社科_东哲",
        "社科_艺术/音乐",
        "娱乐_文学/艺术",
        "技术_英语",
        "技术_写作",
        "技术_摄影",
        "技术_唱歌",
    ]
)
K2_TAGS = ",".join(
    [
        "生活_健身",
        "生活_健康/保养",
        "生活_美食",
        "生活_穿搭",
        "生活_旅行",
    ]
)


def log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def stage1_done() -> bool:
    if not STAGE1_LOG.is_file():
        return False
    try:
        return "night run done" in STAGE1_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def wait_for_stage1(max_hours: int = 14) -> bool:
    deadline = time.time() + max_hours * 3600
    while time.time() < deadline:
        if stage1_done():
            return True
        time.sleep(60)
    return False


def run_digest(label: str, tag: str, limit: int) -> int:
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
        "--retry-failed",
    ]
    log(f"[{label}] start: limit={limit} tag={tag}")
    started = time.time()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with LOG.open("a", encoding="utf-8") as handle:
        code = subprocess.run(args, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env).returncode
    log(f"[{label}] end: exit={code} elapsed={int(time.time() - started)}s")
    return code


def restore_flash() -> None:
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
    log("======== stage2 start (waiting for stage1) ========")
    if not wait_for_stage1():
        log("stage1 未在 14 小时内结束，放弃接力")
        return 0
    log("stage1 done，开始 K1 稳定知识")
    run_digest("knowledge", K1_TAGS, 200)
    pause = random.randint(120, 300)
    log(f"[pause] {pause}s before life batch")
    time.sleep(pause)
    log("开始 K2 生活技能")
    run_digest("life", K2_TAGS, 300)
    restore_flash()
    log("======== stage2 done ========")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

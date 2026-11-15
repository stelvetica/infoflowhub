"""续跑：02:40 熔断后按「心理 → 社科 → 稳定知识 → 生活技能」重跑剩余条目。

修掉了取链竞态（标记逐轮重读）后再跑；已成功的条目会被候选查询自动跳过，
所以从任意一段重跑都是幂等的。结束时把 .env 的 PRIMARY_LLM_MODEL 切回 group/flash。
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
LOG = ROOT / "runtime" / "tmp" / "night_resume_20261115.log"

PSY = "心理_认知/学习方法,心理_自我/情绪分析,心理_他人/关系,心理_职场"
SOCIAL = "社科_历史,社科_哲学/社会/经济/金融"
KNOWLEDGE = ",".join(
    ["社科_科学/科普", "社科_东哲", "社科_艺术/音乐", "娱乐_文学/艺术", "技术_英语", "技术_写作", "技术_摄影", "技术_唱歌"]
)
LIFE = ",".join(["生活_健身", "生活_健康/保养", "生活_美食", "生活_穿搭", "生活_旅行"])


def log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def run(label: str, tag: str, limit: int) -> None:
    args = [
        str(PY), "-m", "apps.laterhub.video_digest",
        "--source", "douyin_favorite",
        "--tag", tag,
        "--limit", str(limit),
        "--keep-temp",
        "--retry-failed",
    ]
    log(f"[start-{label}] limit={limit}")
    started = time.time()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with LOG.open("a", encoding="utf-8") as handle:
        code = subprocess.run(args, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env).returncode
    log(f"[end-{label}] exit={code} elapsed={int(time.time() - started)}s")


def main() -> int:
    log("======== resume start ========")
    run("psychology", PSY, 300)
    time.sleep(random.randint(120, 300))
    run("social", SOCIAL, 120)
    time.sleep(random.randint(120, 300))
    run("knowledge", KNOWLEDGE, 200)
    time.sleep(random.randint(120, 300))
    run("life", LIFE, 300)

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
        log(f"[llm] restored group/flash (changed={changed})")
    except OSError as exc:
        log(f"[llm] restore failed: {exc}")
    log("======== resume done ========")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

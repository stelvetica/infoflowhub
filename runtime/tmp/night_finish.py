"""收尾驱动器：只补第一阶段（心理 → 社科），跑完再统一重试这两组的失败。

用户 2026-11-15 明确：稳定知识/生活技能不跑了，第一阶段跑完即可。
为什么需要循环：批内连续 3 条失败会熔断（防 429 空烧），但熔断会让该组提前收工。
所以每组跑完就查一次剩余 pending，还有就再跑一轮（最多 3 轮；本轮没进展就放弃该组）。
已成功的条目由候选查询自动跳过，任意轮次重跑都幂等。
"""

from __future__ import annotations

import datetime as dt
import os
import random
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(r"C:\Users\ETT\Playground\infoflowhub")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DB = ROOT / "data" / "laterhub.sqlite3"
LOG = ROOT / "runtime" / "tmp" / "night_finish_20261115.log"

PSY = ["心理_认知/学习方法", "心理_自我/情绪分析", "心理_他人/关系", "心理_职场"]
SOCIAL = ["社科_历史", "社科_哲学/社会/经济/金融"]
KNOWLEDGE = ["社科_科学/科普", "社科_东哲", "社科_艺术/音乐", "娱乐_文学/艺术",
             "技术_英语", "技术_写作", "技术_摄影", "技术_唱歌"]
LIFE = ["生活_健身", "生活_健康/保养", "生活_美食", "生活_穿搭", "生活_旅行"]
GROUPS = [("psychology", PSY), ("social", SOCIAL)]


def log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def pending_count(tags: list[str]) -> int:
    """该标签组里还没跑成功的抖音视频数（含 failed，便于重试）。"""
    clause = " or ".join(["instr(','||coalesce(tags,'')||',',?)>0"] * len(tags))
    params = [f",{tag}," for tag in tags]
    with sqlite3.connect(DB) as conn:
        return int(conn.execute(
            f"""select count(*) from links
                where source='douyin_favorite'
                  and url like '%douyin.com/video/%'
                  and digest_status in ('pending','running','failed')
                  and ({clause})""",
            params,
        ).fetchone()[0])


def run(label: str, tags: list[str], limit: int) -> None:
    args = [
        str(PY), "-m", "apps.laterhub.video_digest",
        "--source", "douyin_favorite",
        "--tag", ",".join(tags),
        "--limit", str(limit),
        "--keep-temp",
        "--retry-failed",
    ]
    log(f"[start-{label}] pending={pending_count(tags)} limit={limit}")
    started = time.time()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with LOG.open("a", encoding="utf-8") as handle:
        code = subprocess.run(args, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env).returncode
    log(f"[end-{label}] exit={code} elapsed={int(time.time() - started)}s remaining={pending_count(tags)}")


def main() -> int:
    log("======== finish start ========")
    for name, tags in GROUPS:
        for attempt in range(1, 4):
            before = pending_count(tags)
            if before == 0:
                log(f"[skip-{name}] 已无待跑")
                break
            log(f"[round-{name}] attempt={attempt} pending={before}")
            run(name, tags, 400)
            after = pending_count(tags)
            if after == 0:
                log(f"[done-{name}] 全部完成")
                break
            if after >= before:
                log(f"[stall-{name}] 本轮无进展（{before}→{after}），放弃该组，留给下次")
                break
            time.sleep(random.randint(60, 180))

    # 最后统一重试一轮失败（四组一起）
    all_tags = [tag for _, tags in GROUPS for tag in tags]
    run("retry-all", all_tags, 400)

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
    log("======== finish done ========")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

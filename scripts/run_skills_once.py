"""手动跑一次完整 skill 流程（portfolio-news → portfolio-announcements）。

定时流程（auto_runner）只在早上跑一次；当日失败或想立即重跑时用本脚本。
直接调用 web/services/skill_runner.run_skill——与 auto_runner 同一条代码路径，
所以这里的结果就是定时流程的结果，不会出现「手动能跑、定时不能」的分叉。

用法（务必用 PowerShell 或 cmd，不要在 Git Bash 里跑）：
  uv run python scripts/run_skills_once.py

Git Bash 会做 POSIX 路径转换，把提示词里的 `/skill xxx` 改写成
`C:/Program Files/Git/skill xxx`，slash 命令因此失效（实测踩过）。

注意：本脚本不解决 CLI 自身定位不到 provider 配置的问题——
若报「无法定位 CLI ZCode Built-in Provider Config」，是运行环境缺少
ZCODE_BUILTIN_PROVIDER_CONFIG_FILE，与 skill 无关（详见 TECH_DEBT）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from web.services.skill_runner import SKILLS, run_skill  # noqa: E402

for name in SKILLS:
    print(json.dumps(run_skill(name, cwd=BASE), ensure_ascii=False), flush=True)

"""手动跑一次完整 skill 流程（portfolio-news → portfolio-announcements）。

定时流程（auto_runner）只在早上跑一次；当日失败或想立即重跑时用本脚本。
走 AutoRunner.run_skills_now——与 auto_runner 同一套收尾逻辑，会按一次完整流程
登记状态（flow + last_run_date），所以面板头部与「本次流程」都能反映手动补跑结果，
不会出现「手动能跑、面板仍显示失败/空产物」的分叉。

用法（务必用 PowerShell 或 cmd，不要在 Git Bash 里跑）：
  uv run python scripts/run_skills_once.py

Git Bash 会做 POSIX 路径转换，把提示词里的 `/skill xxx` 改写成
`C:/Program Files/Git/skill xxx`，slash 命令因此失效（实测踩过）。

注意：本脚本不解决 CLI 自身定位不到 provider 配置的问题——
若报「无法定位 CLI ZCode Built-in Provider Config」，是运行环境缺少
ZCODE_BUILTIN_PROVIDER_CONFIG_FILE，与 skill 无关（详见 TECH_DEBT）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from web.services.auto_runner import AutoRunner  # noqa: E402

for item in asyncio.run(AutoRunner().run_skills_now()):
    print(json.dumps(item, ensure_ascii=False), flush=True)

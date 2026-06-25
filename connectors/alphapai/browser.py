from __future__ import annotations

from pathlib import Path

# 蓝宝书已统一走 SharedRunnerSession（见 connectors/_shared/chrome_runner.py）。
# 这里仅保留目标 URL 常量供 runner/web_fetch 引用。
ALPHAPAI_TARGET_URL = "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"
ALPHAPAI_RUNNER_DIR = Path(__file__).resolve().parents[2] / "runtime" / "browser_profiles" / "alphapai-reader-automation"

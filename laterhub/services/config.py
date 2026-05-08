from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
DATA_DIR = RUNTIME_DIR / "data"
LOGS_DIR = RUNTIME_DIR / "logs"
DEBUG_DIR = RUNTIME_DIR / "debug"
BROWSER_PROFILES_DIR = RUNTIME_DIR / "browser_profiles"

DB_PATH = DATA_DIR / "info_hub.db"
LOG_PATH = LOGS_DIR / "run.log"
PW_DOUYIN_PROFILE = BROWSER_PROFILES_DIR / "pw-douyin-profile"
PW_BILI_PROFILE = BROWSER_PROFILES_DIR / "pw-bili-profile"

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = RUNTIME_DIR / "logs"
DEBUG_DIR = RUNTIME_DIR / "debug"
BROWSER_PROFILES_DIR = RUNTIME_DIR / "browser_profiles"
DB_PATH = DATA_DIR / "laterhub.sqlite3"
LOG_PATH = LOGS_DIR / "laterhub.log"

__all__ = [
    "BROWSER_PROFILES_DIR",
    "DATA_DIR",
    "DB_PATH",
    "DEBUG_DIR",
    "ENV_PATH",
    "LOG_PATH",
    "LOGS_DIR",
    "RUNTIME_DIR",
]

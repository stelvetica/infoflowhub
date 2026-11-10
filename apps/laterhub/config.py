from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = RUNTIME_DIR / "logs"
DEBUG_DIR = RUNTIME_DIR / "debug"
BROWSER_PROFILES_DIR = RUNTIME_DIR / "browser_profiles"
DB_PATH = DATA_DIR / "laterhub.sqlite3"
# 稍后读图片附件（/quick 上传）：文件名由服务端按本地时间生成
ATTACHMENTS_DIR = DATA_DIR / "laterhub_attachments"
LOG_PATH = LOGS_DIR / "laterhub.log"
DEEP_SCAN_STATE_PATH = RUNTIME_DIR / "laterhub_deep_scan.json"

__all__ = [
    "ATTACHMENTS_DIR",
    "BROWSER_PROFILES_DIR",
    "DATA_DIR",
    "DB_PATH",
    "DEBUG_DIR",
    "DEEP_SCAN_STATE_PATH",
    "ENV_PATH",
    "LOG_PATH",
    "LOGS_DIR",
    "RUNTIME_DIR",
]

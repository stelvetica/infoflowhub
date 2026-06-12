from __future__ import annotations

import json
import sqlite3
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from connectors._shared.common import CHROME_USER_DATA, kill_chrome_gracefully
from connectors.douyin.favorites import _resolve_default_browser_executable


ALPHAPAI_DEBUG_PORT = 9222
ALPHAPAI_TARGET_URL = "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"
ALPHAPAI_PROFILE_NAME = "Default"
ALPHAPAI_LEGACY_RUNNER_DIR = Path(__file__).resolve().parents[2] / "runtime" / "browser_profiles" / "alphapai-runner"
ALPHAPAI_RUNNER_DIR = Path(__file__).resolve().parents[2] / "runtime" / "browser_profiles" / "alphapai-reader-automation"
ALPHAPAI_RUNNER_PROFILE_DIR = ALPHAPAI_RUNNER_DIR / ALPHAPAI_PROFILE_NAME
ALPHAPAI_RUNNER_META_PATH = ALPHAPAI_RUNNER_DIR / ".meta.json"
ALPHAPAI_RUNNER_REBUILD_INTERVAL_SECONDS = 3 * 24 * 60 * 60
ROOT_FILES_TO_COPY = ("Local State", "First Run", "Last Version", "Last Browser")
PROFILE_FILES_TO_COPY = (
    "Bookmarks",
    "Cookies",
    "History",
    "Preferences",
    "Secure Preferences",
    "Web Data",
    "Login Data",
    "Favicons",
    "Top Sites",
)
PROFILE_DIRS_TO_COPY = (
    "Network",
    "Sessions",
    "Session Storage",
    "Local Storage",
    "IndexedDB",
    "Service Worker",
    "Shared Storage",
    "WebStorage",
    "Code Cache",
)


def _debug_url(path: str) -> str:
    return f"http://127.0.0.1:{ALPHAPAI_DEBUG_PORT}{path}"


def _read_json(url: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def is_alphapai_debug_browser_ready() -> bool:
    payload = _read_json(_debug_url("/json/version"))
    return isinstance(payload, dict) and bool(payload.get("Browser"))


def wait_for_alphapai_debug_browser(timeout_seconds: int = 20) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_alphapai_debug_browser_ready():
            return True
        time.sleep(0.5)
    return False


def list_debug_tabs() -> list[dict]:
    payload = _read_json(_debug_url("/json"))
    return payload if isinstance(payload, list) else []


def _is_runner_profile_ready() -> bool:
    cookies = ALPHAPAI_RUNNER_PROFILE_DIR / "Network" / "Cookies"
    prefs = ALPHAPAI_RUNNER_PROFILE_DIR / "Preferences"
    return cookies.exists() and cookies.stat().st_size > 0 and prefs.exists()


def _remove_runner_profile() -> None:
    try:
        if ALPHAPAI_RUNNER_DIR.exists():
            shutil.rmtree(ALPHAPAI_RUNNER_DIR, ignore_errors=True)
    except OSError:
        pass


def _migrate_legacy_runner_dir() -> None:
    if ALPHAPAI_RUNNER_DIR.exists() or not ALPHAPAI_LEGACY_RUNNER_DIR.exists():
        return
    try:
        shutil.move(str(ALPHAPAI_LEGACY_RUNNER_DIR), str(ALPHAPAI_RUNNER_DIR))
    except OSError:
        pass


def _ensure_all_chrome_processes_stopped(timeout_seconds: int = 20) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = subprocess.run(
            ["tasklist", "/fi", "imagename eq chrome.exe"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if "chrome.exe" not in output.lower():
            return True
        time.sleep(0.5)
    return False


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _copy_path(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _copy_sqlite_best_effort(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        return
    except OSError:
        pass

    read_uri = f"file:{src.as_posix()}?mode=ro"
    dest_conn = sqlite3.connect(str(dst))
    try:
        src_conn = sqlite3.connect(read_uri, uri=True)
        try:
            src_conn.backup(dest_conn)
        finally:
            src_conn.close()
    finally:
        dest_conn.close()


def _now_ts() -> int:
    return int(time.time())


def _read_runner_meta() -> dict:
    if not ALPHAPAI_RUNNER_META_PATH.exists():
        return {}
    try:
        return json.loads(ALPHAPAI_RUNNER_META_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_runner_meta(*, rebuilt_at: int) -> None:
    payload = {
        "profile_name": ALPHAPAI_PROFILE_NAME,
        "source_profile_dir": str(CHROME_USER_DATA / ALPHAPAI_PROFILE_NAME),
        "rebuilt_at": rebuilt_at,
    }
    ALPHAPAI_RUNNER_META_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def should_rebuild_runner_profile() -> bool:
    if not _is_runner_profile_ready():
        return True
    meta = _read_runner_meta()
    rebuilt_at = int(meta.get("rebuilt_at") or 0)
    if rebuilt_at <= 0:
        return True
    return (_now_ts() - rebuilt_at) >= ALPHAPAI_RUNNER_REBUILD_INTERVAL_SECONDS


def prepare_alphapai_runner_profile() -> None:
    _migrate_legacy_runner_dir()
    if not should_rebuild_runner_profile():
        return

    _remove_runner_profile()
    ALPHAPAI_RUNNER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    for name in ROOT_FILES_TO_COPY:
        _copy_path(CHROME_USER_DATA / name, ALPHAPAI_RUNNER_DIR / name)

    source_profile_dir = CHROME_USER_DATA / ALPHAPAI_PROFILE_NAME
    if not source_profile_dir.exists():
        raise RuntimeError(f"未找到系统 Chrome 配置目录: {source_profile_dir}")

    for name in PROFILE_FILES_TO_COPY:
        src = source_profile_dir / name
        dst = ALPHAPAI_RUNNER_PROFILE_DIR / name
        if name in {"Cookies", "History", "Web Data", "Login Data", "Favicons", "Top Sites"}:
            _copy_sqlite_best_effort(src, dst)
        else:
            _copy_path(src, dst)
    for name in PROFILE_DIRS_TO_COPY:
        src = source_profile_dir / name
        dst = ALPHAPAI_RUNNER_PROFILE_DIR / name
        if name == "Network":
            dst.mkdir(parents=True, exist_ok=True)
            _copy_sqlite_best_effort(src / "Cookies", dst / "Cookies")
        else:
            _copy_path(src, dst)

    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        _safe_unlink(ALPHAPAI_RUNNER_DIR / lock_name)
        _safe_unlink(ALPHAPAI_RUNNER_PROFILE_DIR / lock_name)

    if not _is_runner_profile_ready():
        raise RuntimeError("专用浏览器 profile 初始化失败，未能复制出有效 Cookies")
    _write_runner_meta(rebuilt_at=_now_ts())


def rebuild_alphapai_runner_profile() -> None:
    if not kill_chrome_gracefully():
        raise RuntimeError("Chrome 正在运行且无法关闭，无法重建蓝宝书专用浏览器 profile")
    if not _ensure_all_chrome_processes_stopped():
        raise RuntimeError("Chrome 进程未完全退出，无法重建蓝宝书专用浏览器 profile")
    _remove_runner_profile()
    prepare_alphapai_runner_profile()


def try_rebuild_alphapai_runner_profile() -> bool:
    try:
        rebuild_alphapai_runner_profile()
        return True
    except Exception:
        return False


def ensure_alphapai_debug_browser() -> None:
    if is_alphapai_debug_browser_ready():
        if should_rebuild_runner_profile():
            try_rebuild_alphapai_runner_profile()
        else:
            return
        if is_alphapai_debug_browser_ready():
            return
    if should_rebuild_runner_profile():
        if not try_rebuild_alphapai_runner_profile() and _is_runner_profile_ready():
            pass
        elif not _is_runner_profile_ready():
            rebuild_alphapai_runner_profile()
    elif not _is_runner_profile_ready():
        if not kill_chrome_gracefully():
            raise RuntimeError("Chrome 正在运行且无法关闭，无法初始化蓝宝书专用浏览器 profile")
        if not _ensure_all_chrome_processes_stopped():
            raise RuntimeError("Chrome 进程未完全退出，无法初始化蓝宝书专用浏览器 profile")
    prepare_alphapai_runner_profile()

    _, chrome_executable = _resolve_default_browser_executable()
    args = [
        chrome_executable,
        f"--remote-debugging-port={ALPHAPAI_DEBUG_PORT}",
        f"--user-data-dir={ALPHAPAI_RUNNER_DIR}",
        f"--profile-directory={ALPHAPAI_PROFILE_NAME}",
        "--no-first-run",
        "--disable-popup-blocking",
        "--start-maximized",
        ALPHAPAI_TARGET_URL,
    ]
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    if not wait_for_alphapai_debug_browser():
        raise RuntimeError("Chrome 已启动，但远程调试端口未就绪")


def force_rebuild_alphapai_debug_browser() -> None:
    rebuild_alphapai_runner_profile()
    if is_alphapai_debug_browser_ready():
        return
    ensure_alphapai_debug_browser()


def find_alphapai_tab_url() -> str:
    for tab in list_debug_tabs():
        url = str(tab.get("url") or "").strip()
        if "alphapai-web.rabyte.cn" in url:
            return url
    return ALPHAPAI_TARGET_URL


def connect_over_cdp_endpoint() -> str:
    return _debug_url("")

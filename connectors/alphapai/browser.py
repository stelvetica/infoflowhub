from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from connectors._shared.chrome_runner import (
    close_debug_browser,
    connect_over_cdp,
    ensure_debug_browser,
    find_tab_url,
    force_rebuild_debug_browser,
    is_debug_browser_ready,
    is_runner_profile_ready,
    list_debug_tabs,
    prepare_runner_profile,
    rebuild_runner_profile,
    should_rebuild_runner_profile,
    try_prepare_runner_profile,
    try_rebuild_runner_profile,
    wait_for_debug_browser,
    _write_runner_meta,
)
from connectors._shared.common import CHROME_USER_DATA


ALPHAPAI_DEBUG_PORT = 9222
ALPHAPAI_TARGET_URL = "https://alphapai-web.rabyte.cn/reading/home/market-report/detail"
ALPHAPAI_PROFILE_NAME = "Default"
ALPHAPAI_LEGACY_RUNNER_DIR = Path(__file__).resolve().parents[2] / "runtime" / "browser_profiles" / "alphapai-runner"
ALPHAPAI_RUNNER_DIR = Path(__file__).resolve().parents[2] / "runtime" / "browser_profiles" / "alphapai-reader-automation"
ALPHAPAI_RUNNER_PROFILE_DIR = ALPHAPAI_RUNNER_DIR / ALPHAPAI_PROFILE_NAME
ALPHAPAI_RUNNER_META_PATH = ALPHAPAI_RUNNER_DIR / ".meta.json"
ALPHAPAI_RUNNER_REBUILD_INTERVAL_SECONDS = 24 * 60 * 60
ROOT_FILES_TO_COPY = (
    "Local State",
    "First Run",
    "Last Version",
    "Last Browser",
)
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


def _now_ts() -> int:
    return int(time.time())


def _source_profile_dir() -> Path:
    return CHROME_USER_DATA / ALPHAPAI_PROFILE_NAME


def _migrate_legacy_runner_dir() -> None:
    if ALPHAPAI_RUNNER_DIR.exists() or not ALPHAPAI_LEGACY_RUNNER_DIR.exists():
        return
    try:
        shutil.move(str(ALPHAPAI_LEGACY_RUNNER_DIR), str(ALPHAPAI_RUNNER_DIR))
    except OSError:
        pass


def is_alphapai_debug_browser_ready() -> bool:
    return is_debug_browser_ready(ALPHAPAI_DEBUG_PORT)


def wait_for_alphapai_debug_browser(timeout_seconds: int = 20) -> bool:
    return wait_for_debug_browser(ALPHAPAI_DEBUG_PORT, timeout_seconds=timeout_seconds)


def list_alphapai_debug_tabs() -> list[dict]:
    return list_debug_tabs(ALPHAPAI_DEBUG_PORT)


def close_alphapai_debug_browser() -> None:
    close_debug_browser(ALPHAPAI_RUNNER_DIR, ALPHAPAI_DEBUG_PORT)


def _is_runner_profile_ready() -> bool:
    return is_runner_profile_ready(ALPHAPAI_RUNNER_PROFILE_DIR)


def should_rebuild_alphapai_runner_profile() -> bool:
    return should_rebuild_runner_profile(
        ALPHAPAI_RUNNER_PROFILE_DIR,
        ALPHAPAI_RUNNER_DIR,
        ALPHAPAI_RUNNER_REBUILD_INTERVAL_SECONDS,
    )


def prepare_alphapai_runner_profile() -> None:
    _migrate_legacy_runner_dir()
    prepare_runner_profile(
        _source_profile_dir(),
        ALPHAPAI_RUNNER_DIR,
        ALPHAPAI_PROFILE_NAME,
        root_files=ROOT_FILES_TO_COPY,
        profile_files=PROFILE_FILES_TO_COPY,
        profile_dirs=PROFILE_DIRS_TO_COPY,
    )
    _write_runner_meta(
        ALPHAPAI_RUNNER_DIR,
        profile_name=ALPHAPAI_PROFILE_NAME,
        source_profile_dir=_source_profile_dir(),
        rebuilt_at=_now_ts(),
    )


def rebuild_alphapai_runner_profile() -> None:
    _migrate_legacy_runner_dir()
    rebuild_runner_profile(
        _source_profile_dir(),
        ALPHAPAI_RUNNER_DIR,
        ALPHAPAI_PROFILE_NAME,
        root_files=ROOT_FILES_TO_COPY,
        profile_files=PROFILE_FILES_TO_COPY,
        profile_dirs=PROFILE_DIRS_TO_COPY,
    )
    _write_runner_meta(
        ALPHAPAI_RUNNER_DIR,
        profile_name=ALPHAPAI_PROFILE_NAME,
        source_profile_dir=_source_profile_dir(),
        rebuilt_at=_now_ts(),
    )


def try_rebuild_alphapai_runner_profile() -> bool:
    try:
        rebuild_alphapai_runner_profile()
        return True
    except Exception:
        return False


def try_prepare_alphapai_runner_profile() -> bool:
    try:
        from connectors._shared.chrome_runner import _remove_runner_dir
        _remove_runner_dir(ALPHAPAI_RUNNER_DIR)
        prepare_alphapai_runner_profile()
        return _is_runner_profile_ready()
    except Exception:
        return False


def ensure_alphapai_debug_browser() -> None:
    _migrate_legacy_runner_dir()
    ensure_debug_browser(
        ALPHAPAI_RUNNER_DIR,
        ALPHAPAI_PROFILE_NAME,
        ALPHAPAI_DEBUG_PORT,
        ALPHAPAI_TARGET_URL,
        source_profile_dir=_source_profile_dir(),
        rebuild_interval=ALPHAPAI_RUNNER_REBUILD_INTERVAL_SECONDS,
        headless=False,
        root_files=ROOT_FILES_TO_COPY,
        profile_files=PROFILE_FILES_TO_COPY,
        profile_dirs=PROFILE_DIRS_TO_COPY,
    )
    _write_runner_meta(
        ALPHAPAI_RUNNER_DIR,
        profile_name=ALPHAPAI_PROFILE_NAME,
        source_profile_dir=_source_profile_dir(),
        rebuilt_at=_now_ts(),
    )


def force_rebuild_alphapai_debug_browser() -> None:
    _migrate_legacy_runner_dir()
    force_rebuild_debug_browser(
        ALPHAPAI_RUNNER_DIR,
        ALPHAPAI_PROFILE_NAME,
        ALPHAPAI_DEBUG_PORT,
        ALPHAPAI_TARGET_URL,
        source_profile_dir=_source_profile_dir(),
        rebuild_interval=ALPHAPAI_RUNNER_REBUILD_INTERVAL_SECONDS,
        headless=False,
        root_files=ROOT_FILES_TO_COPY,
        profile_files=PROFILE_FILES_TO_COPY,
        profile_dirs=PROFILE_DIRS_TO_COPY,
    )
    _write_runner_meta(
        ALPHAPAI_RUNNER_DIR,
        profile_name=ALPHAPAI_PROFILE_NAME,
        source_profile_dir=_source_profile_dir(),
        rebuilt_at=_now_ts(),
    )


def find_alphapai_tab_url() -> str:
    return find_tab_url(ALPHAPAI_DEBUG_PORT, "alphapai-web.rabyte.cn", ALPHAPAI_TARGET_URL)


def connect_over_cdp_endpoint() -> str:
    return connect_over_cdp(ALPHAPAI_DEBUG_PORT)

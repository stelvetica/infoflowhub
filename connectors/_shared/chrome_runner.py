from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import winreg
from pathlib import Path
from urllib.error import HTTPError, URLError

from connectors._shared.common import CHROME_USER_DATA, kill_chrome_gracefully


DEFAULT_ROOT_FILES_TO_COPY = ("Local State", "First Run", "Last Version", "Last Browser")
DEFAULT_PROFILE_FILES_TO_COPY = (
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
DEFAULT_PROFILE_DIRS_TO_COPY = (
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
DEFAULT_REBUILD_INTERVAL_SECONDS = 24 * 60 * 60


WINDOWS_BROWSER_PATHS = {
    "ChromeHTML": [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ],
    "MSEdgeHTM": [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ],
    "BraveHTML": [
        Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ],
    "VivaldiHTM": [
        Path(r"C:\Program Files\Vivaldi\Application\vivaldi.exe"),
        Path(r"C:\Program Files (x86)\Vivaldi\Application\vivaldi.exe"),
    ],
}


def _resolve_default_browser_executable() -> tuple[str, str]:
    prog_id = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        prog_id = ""

    for candidate in WINDOWS_BROWSER_PATHS.get(prog_id, []):
        if candidate.exists():
            return prog_id or "unknown", str(candidate)

    for fallback_prog_id in ("ChromeHTML", "MSEdgeHTM", "BraveHTML", "VivaldiHTM"):
        for candidate in WINDOWS_BROWSER_PATHS[fallback_prog_id]:
            if candidate.exists():
                return fallback_prog_id, str(candidate)

    raise RuntimeError("未找到可供 Playwright 启动的 Chromium 浏览器，请先安装 Chrome、Edge、Brave 或 Vivaldi")



def _debug_url(debug_port: int, path: str) -> str:
    return f"http://127.0.0.1:{debug_port}{path}"


def _read_json(url: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def is_debug_browser_ready(debug_port: int) -> bool:
    payload = _read_json(_debug_url(debug_port, "/json/version"))
    return isinstance(payload, dict) and bool(payload.get("Browser"))


def wait_for_debug_browser(debug_port: int, timeout_seconds: int = 20) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_debug_browser_ready(debug_port):
            return True
        time.sleep(0.5)
    return False


def list_debug_tabs(debug_port: int) -> list[dict]:
    payload = _read_json(_debug_url(debug_port, "/json"))
    return payload if isinstance(payload, list) else []


def connect_over_cdp(debug_port: int) -> str:
    return _debug_url(debug_port, "")


def find_tab_url(debug_port: int, keyword: str, default_url: str) -> str:
    for tab in list_debug_tabs(debug_port):
        url = str(tab.get("url") or "").strip()
        if keyword in url:
            return url
    return default_url


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _remove_runner_dir(runner_dir: Path) -> None:
    try:
        if runner_dir.exists():
            shutil.rmtree(runner_dir, ignore_errors=True)
    except OSError:
        pass


def _copy_path(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    try:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def _copy_sqlite_best_effort(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        return True
    except OSError:
        pass

    read_uri = f"file:{src.as_posix()}?mode=ro"
    try:
        dest_conn = sqlite3.connect(str(dst))
        try:
            src_conn = sqlite3.connect(read_uri, uri=True)
            try:
                src_conn.backup(dest_conn)
                return True
            finally:
                src_conn.close()
        finally:
            dest_conn.close()
    except sqlite3.Error:
        return False


def _ensure_minimal_preferences(profile_dir: Path) -> None:
    prefs = profile_dir / "Preferences"
    if prefs.exists():
        return
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text("{}", encoding="utf-8")


def _now_ts() -> int:
    return int(time.time())


def _read_runner_meta(runner_dir: Path) -> dict:
    meta_path = runner_dir / ".meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_runner_meta(
    runner_dir: Path,
    *,
    profile_name: str,
    source_profile_dir: Path,
    rebuilt_at: int,
) -> None:
    meta_path = runner_dir / ".meta.json"
    payload = {
        "profile_name": profile_name,
        "source_profile_dir": str(source_profile_dir),
        "rebuilt_at": rebuilt_at,
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def is_runner_profile_ready(runner_profile_dir: Path) -> bool:
    cookies = runner_profile_dir / "Network" / "Cookies"
    prefs = runner_profile_dir / "Preferences"
    return cookies.exists() and cookies.stat().st_size > 0 and prefs.exists()


def should_rebuild_runner_profile(
    runner_profile_dir: Path,
    runner_dir: Path,
    rebuild_interval: int = DEFAULT_REBUILD_INTERVAL_SECONDS,
) -> bool:
    if not is_runner_profile_ready(runner_profile_dir):
        return True
    meta = _read_runner_meta(runner_dir)
    rebuilt_at = int(meta.get("rebuilt_at") or 0)
    if rebuilt_at <= 0:
        return True
    return (_now_ts() - rebuilt_at) >= rebuild_interval


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


def _force_kill_chrome_powershell() -> bool:
    """使用 PowerShell Stop-Process -Force 兜底终止残留 chrome.exe 进程"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue"],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return _ensure_all_chrome_processes_stopped(timeout_seconds=5)

    time.sleep(2)
    if _ensure_all_chrome_processes_stopped(timeout_seconds=8):
        return True

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process -Name chrome -ErrorAction SilentlyContinue | Stop-Process -Force"],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass
    time.sleep(2)
    return _ensure_all_chrome_processes_stopped(timeout_seconds=5)


def _stop_chrome_for_profile_copy() -> None:
    if not kill_chrome_gracefully():
        raise RuntimeError("Chrome 正在运行且无法关闭，无法复制浏览器 profile")
    if not _ensure_all_chrome_processes_stopped():
        if not _force_kill_chrome_powershell():
            raise RuntimeError("Chrome 进程未完全退出，无法复制浏览器 profile")


def prepare_runner_profile(
    source_profile_dir: Path,
    runner_dir: Path,
    profile_name: str,
    *,
    root_files: tuple[str, ...] = DEFAULT_ROOT_FILES_TO_COPY,
    profile_files: tuple[str, ...] = DEFAULT_PROFILE_FILES_TO_COPY,
    profile_dirs: tuple[str, ...] = DEFAULT_PROFILE_DIRS_TO_COPY,
) -> None:
    """将系统 Chrome profile 复制到隔离的 runner 目录。"""
    runner_profile_dir = runner_dir / profile_name
    _remove_runner_dir(runner_dir)
    runner_profile_dir.mkdir(parents=True, exist_ok=True)

    for name in root_files:
        _copy_path(CHROME_USER_DATA / name, runner_dir / name)

    if not source_profile_dir.exists():
        raise RuntimeError(f"未找到源 Chrome profile 目录: {source_profile_dir}")

    sqlite_files = {"Cookies", "History", "Web Data", "Login Data", "Favicons", "Top Sites"}
    for name in profile_files:
        src = source_profile_dir / name
        dst = runner_profile_dir / name
        if name in sqlite_files:
            _copy_sqlite_best_effort(src, dst)
        else:
            _copy_path(src, dst)

    for name in profile_dirs:
        src = source_profile_dir / name
        dst = runner_profile_dir / name
        if name == "Network":
            dst.mkdir(parents=True, exist_ok=True)
            _copy_sqlite_best_effort(src / "Cookies", dst / "Cookies")
        else:
            _copy_path(src, dst)

    _ensure_minimal_preferences(runner_profile_dir)

    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        _safe_unlink(runner_dir / lock_name)
        _safe_unlink(runner_profile_dir / lock_name)

    if not is_runner_profile_ready(runner_profile_dir):
        raise RuntimeError("专用浏览器 profile 初始化失败，未能复制出有效 Cookies")


def rebuild_runner_profile(
    source_profile_dir: Path,
    runner_dir: Path,
    profile_name: str,
    *,
    root_files: tuple[str, ...] = DEFAULT_ROOT_FILES_TO_COPY,
    profile_files: tuple[str, ...] = DEFAULT_PROFILE_FILES_TO_COPY,
    profile_dirs: tuple[str, ...] = DEFAULT_PROFILE_DIRS_TO_COPY,
) -> None:
    _stop_chrome_for_profile_copy()
    prepare_runner_profile(
        source_profile_dir,
        runner_dir,
        profile_name,
        root_files=root_files,
        profile_files=profile_files,
        profile_dirs=profile_dirs,
    )


def try_prepare_runner_profile(
    source_profile_dir: Path,
    runner_dir: Path,
    profile_name: str,
    *,
    root_files: tuple[str, ...] = DEFAULT_ROOT_FILES_TO_COPY,
    profile_files: tuple[str, ...] = DEFAULT_PROFILE_FILES_TO_COPY,
    profile_dirs: tuple[str, ...] = DEFAULT_PROFILE_DIRS_TO_COPY,
) -> bool:
    try:
        _remove_runner_dir(runner_dir)
        prepare_runner_profile(
            source_profile_dir,
            runner_dir,
            profile_name,
            root_files=root_files,
            profile_files=profile_files,
            profile_dirs=profile_dirs,
        )
        return is_runner_profile_ready(runner_dir / profile_name)
    except Exception:
        return False


def try_rebuild_runner_profile(
    source_profile_dir: Path,
    runner_dir: Path,
    profile_name: str,
    *,
    root_files: tuple[str, ...] = DEFAULT_ROOT_FILES_TO_COPY,
    profile_files: tuple[str, ...] = DEFAULT_PROFILE_FILES_TO_COPY,
    profile_dirs: tuple[str, ...] = DEFAULT_PROFILE_DIRS_TO_COPY,
) -> bool:
    try:
        rebuild_runner_profile(
            source_profile_dir,
            runner_dir,
            profile_name,
            root_files=root_files,
            profile_files=profile_files,
            profile_dirs=profile_dirs,
        )
        return True
    except Exception:
        return False


def ensure_debug_browser(
    runner_dir: Path,
    profile_name: str,
    debug_port: int,
    start_url: str,
    *,
    source_profile_dir: Path,
    rebuild_interval: int = DEFAULT_REBUILD_INTERVAL_SECONDS,
    headless: bool = True,
    extra_args: list[str] | None = None,
    root_files: tuple[str, ...] = DEFAULT_ROOT_FILES_TO_COPY,
    profile_files: tuple[str, ...] = DEFAULT_PROFILE_FILES_TO_COPY,
    profile_dirs: tuple[str, ...] = DEFAULT_PROFILE_DIRS_TO_COPY,
) -> None:
    runner_profile_dir = runner_dir / profile_name

    if is_debug_browser_ready(debug_port):
        if not should_rebuild_runner_profile(runner_profile_dir, runner_dir, rebuild_interval):
            return

    if should_rebuild_runner_profile(runner_profile_dir, runner_dir, rebuild_interval):
        if try_prepare_runner_profile(
            source_profile_dir,
            runner_dir,
            profile_name,
            root_files=root_files,
            profile_files=profile_files,
            profile_dirs=profile_dirs,
        ):
            pass
        elif not try_rebuild_runner_profile(
            source_profile_dir,
            runner_dir,
            profile_name,
            root_files=root_files,
            profile_files=profile_files,
            profile_dirs=profile_dirs,
        ) and is_runner_profile_ready(runner_profile_dir):
            pass
        elif not is_runner_profile_ready(runner_profile_dir):
            rebuild_runner_profile(
                source_profile_dir,
                runner_dir,
                profile_name,
                root_files=root_files,
                profile_files=profile_files,
                profile_dirs=profile_dirs,
            )
    elif not is_runner_profile_ready(runner_profile_dir):
        _stop_chrome_for_profile_copy()
        prepare_runner_profile(
            source_profile_dir,
            runner_dir,
            profile_name,
            root_files=root_files,
            profile_files=profile_files,
            profile_dirs=profile_dirs,
        )

    _, chrome_executable = _resolve_default_browser_executable()
    # 显式用绝对路径，避免相对路径在 Windows 上导致 Chrome 无法创建数据目录
    runner_dir_abs = str(Path(runner_dir).resolve())
    args = [
        chrome_executable,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={runner_dir_abs}",
        f"--profile-directory={profile_name}",
        "--no-first-run",
        "--disable-popup-blocking",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
    ]
    if headless:
        args.append("--headless=new")
    if extra_args:
        args.extend(extra_args)
    args.append("--start-maximized")
    args.append(start_url)

    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    if not wait_for_debug_browser(debug_port):
        raise RuntimeError("Chrome 已启动，但远程调试端口未就绪")


def force_rebuild_debug_browser(
    runner_dir: Path,
    profile_name: str,
    debug_port: int,
    start_url: str,
    *,
    source_profile_dir: Path,
    rebuild_interval: int = DEFAULT_REBUILD_INTERVAL_SECONDS,
    headless: bool = True,
    extra_args: list[str] | None = None,
    root_files: tuple[str, ...] = DEFAULT_ROOT_FILES_TO_COPY,
    profile_files: tuple[str, ...] = DEFAULT_PROFILE_FILES_TO_COPY,
    profile_dirs: tuple[str, ...] = DEFAULT_PROFILE_DIRS_TO_COPY,
) -> None:
    rebuild_runner_profile(
        source_profile_dir,
        runner_dir,
        profile_name,
        root_files=root_files,
        profile_files=profile_files,
        profile_dirs=profile_dirs,
    )
    if is_debug_browser_ready(debug_port):
        return
    ensure_debug_browser(
        runner_dir,
        profile_name,
        debug_port,
        start_url,
        source_profile_dir=source_profile_dir,
        rebuild_interval=rebuild_interval,
        headless=headless,
        extra_args=extra_args,
        root_files=root_files,
        profile_files=profile_files,
        profile_dirs=profile_dirs,
    )


def close_debug_browser(runner_dir: Path, debug_port: int) -> None:
    for tab in list_debug_tabs(debug_port):
        tab_id = str(tab.get("id") or "").strip()
        if not tab_id:
            continue
        try:
            urllib.request.urlopen(_debug_url(debug_port, f"/json/close/{tab_id}"), timeout=2).close()
        except (HTTPError, URLError, TimeoutError, OSError):
            pass

    runner_dir_text = str(runner_dir).lower()
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return
    if isinstance(payload, dict):
        processes = [payload]
    elif isinstance(payload, list):
        processes = payload
    else:
        processes = []
    for item in processes:
        command_line = str(item.get("CommandLine") or "").lower()
        if runner_dir_text not in command_line:
            continue
        pid = str(item.get("ProcessId") or "").strip()
        if not pid:
            continue
        try:
            subprocess.run(["taskkill", "/pid", pid, "/t", "/f"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


SHARED_RUNNER_DIR = Path(__file__).resolve().parents[2] / "runtime" / "browser_profiles" / "shared-runner"
SHARED_RUNNER_DEBUG_PORT = 9280
SHARED_RUNNER_PROFILE_NAME = "Default"


class SharedRunnerSession:
    """抓取会话级常驻的共享 Chrome runner。

    一次 fetch 周期内：start() 起一次 Chrome（复制一份 Default profile），
    各站点抓取通过 acquire_page() 拿到新页面，最后 shutdown() 关闭浏览器。
    全程共用一个 Chrome 进程、一个端口、一份 profile 副本。
    """

    def __init__(
        self,
        *,
        source_profile_dir: Path,
        runner_dir: Path = SHARED_RUNNER_DIR,
        debug_port: int = SHARED_RUNNER_DEBUG_PORT,
        profile_name: str = SHARED_RUNNER_PROFILE_NAME,
        rebuild_interval: int = DEFAULT_REBUILD_INTERVAL_SECONDS,
        headless: bool = True,
        extra_args: list[str] | None = None,
    ) -> None:
        self.source_profile_dir = source_profile_dir
        self.runner_dir = runner_dir
        self.debug_port = debug_port
        self.profile_name = profile_name
        self.rebuild_interval = rebuild_interval
        self.headless = headless
        self.extra_args = extra_args
        self._playwright = None
        self._browser = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        ensure_debug_browser(
            self.runner_dir,
            self.profile_name,
            self.debug_port,
            "about:blank",
            source_profile_dir=self.source_profile_dir,
            rebuild_interval=self.rebuild_interval,
            headless=self.headless,
            extra_args=self.extra_args,
        )
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(connect_over_cdp(self.debug_port))
        self._started = True

    def acquire_page(self):
        if not self._started or self._browser is None:
            raise RuntimeError("SharedRunnerSession 未启动，请先调用 start()")
        context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        return context.new_page()

    def shutdown(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._started:
            close_debug_browser(self.runner_dir, self.debug_port)
            self._started = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()
        return False


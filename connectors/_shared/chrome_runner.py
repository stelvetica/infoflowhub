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

from connectors._shared.common import CHROME_USER_DATA, is_chrome_running, kill_chrome_gracefully


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
DEFAULT_REBUILD_INTERVAL_SECONDS = 7 * 24 * 60 * 60

# 全局重建时钟：所有 runner 副本（web/laterhub/alphapai/bilibili）共用一份
# rebuilt_at，任一副本重建即刷新——避免各目录独立计时导致"每隔一两天就有
# 某个副本到期重建、反复强杀用户 Chrome"。重建间隔也统一放宽到 7 天。
REBUILD_CLOCK_PATH = Path(__file__).resolve().parents[2] / "runtime" / "browser_profiles" / ".rebuild_clock.json"


def _read_rebuild_clock() -> int:
    try:
        return int(json.loads(REBUILD_CLOCK_PATH.read_text(encoding="utf-8")).get("rebuilt_at") or 0)
    except (OSError, ValueError):
        return 0


def _write_rebuild_clock() -> None:
    try:
        REBUILD_CLOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        REBUILD_CLOCK_PATH.write_text(json.dumps({"rebuilt_at": _now_ts()}), encoding="utf-8")
    except OSError as exc:
        print(f"[WARN] 写入全局重建时钟失败: {exc}")

# 抓取期间会关掉用户常用 Chrome，抓完重开时默认带上 InfoFlowHub 控制台链接。
INFOFLOW_WEB_URL = "http://127.0.0.1:18421/"


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
    # 全局统一用 Chrome：只要本机装了 Chrome 就优先选它，不跟随系统默认浏览器
    # （系统默认可能是 Edge，会与复制的 Chrome profile/共享 profile 不兼容）。
    for prog_id in ("ChromeHTML", "MSEdgeHTM", "BraveHTML", "VivaldiHTM"):
        for candidate in WINDOWS_BROWSER_PATHS[prog_id]:
            if candidate.exists():
                return prog_id, str(candidate)

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


def _copy_sqlite_best_effort(src: Path, dst: Path) -> tuple[bool, str]:
    """返回 (是否成功, 失败原因)。成功时原因为空串。"""
    if not src.exists():
        return False, f"源文件不存在: {src}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        return True, ""
    except OSError as exc:
        copy_err = f"shutil.copy2 失败({exc})"

    # 源被持锁时走 sqlite3.backup 兜底（只读句柄绕过文件锁）
    read_uri = f"file:{src.as_posix()}?mode=ro"
    try:
        dest_conn = sqlite3.connect(str(dst))
        try:
            src_conn = sqlite3.connect(read_uri, uri=True)
            try:
                src_conn.backup(dest_conn)
                return True, ""
            finally:
                src_conn.close()
        finally:
            dest_conn.close()
    except sqlite3.Error as exc:
        return False, f"{copy_err}; sqlite3.backup 失败({exc})"


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
    # 统一计时：读全局重建时钟而非各 runner 自己的 meta，任一副本重建后
    # 全体副本在间隔内都不再重建（免反复强杀用户 Chrome）。
    rebuilt_at = _read_rebuild_clock()
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
            encoding="utf-8",
            errors="ignore",
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


def _reopen_user_chrome() -> None:
    """抓取结束后重开用户常用 Chrome（profile 复制时被关掉的）。

    带 InfoFlowHub 控制台链接打开；若 web 服务未起，浏览器会显示无法连接，
    但 Chrome 窗口本身恢复。ponytail: 直接带 URL 启动，Chrome 会打开该页；
    天花板：无法恢复用户抓取前的精确窗口/标签状态；若用户本就无 Chrome
    在跑（_chrome_was_running=False），不会调用此函数。
    """
    try:
        _, chrome_executable = _resolve_default_browser_executable()
        subprocess.Popen(
            [chrome_executable, INFOFLOW_WEB_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception:
        pass


def prepare_runner_profile(
    source_profile_dir: Path,
    runner_dir: Path,
    profile_name: str,
    *,
    root_files: tuple[str, ...] = DEFAULT_ROOT_FILES_TO_COPY,
    profile_files: tuple[str, ...] = DEFAULT_PROFILE_FILES_TO_COPY,
    profile_dirs: tuple[str, ...] = DEFAULT_PROFILE_DIRS_TO_COPY,
) -> None:
    """将系统 Chrome profile 复制到隔离的 runner 目录。

    复制源 profile 前必须先关闭 Chrome——源 Network/Cookies 被 Chrome 持锁时
    任何复制手段（shutil / sqlite3.backup）都会失败。把关 Chrome 下沉到这里，
    所有调用方（try_prepare / try_rebuild / ensure_debug_browser）自动受保护，
    不再需要各自记得关 Chrome（懒汉：一处 guard 覆盖全部调用方）。
    """
    _stop_chrome_for_profile_copy()
    runner_profile_dir = runner_dir / profile_name
    _remove_runner_dir(runner_dir)
    runner_profile_dir.mkdir(parents=True, exist_ok=True)

    for name in root_files:
        _copy_path(CHROME_USER_DATA / name, runner_dir / name)

    if not source_profile_dir.exists():
        raise RuntimeError(f"未找到源 Chrome profile 目录: {source_profile_dir}")

    sqlite_files = {"Cookies", "History", "Web Data", "Login Data", "Favicons", "Top Sites"}
    # 登录态命脉：源存在却复制失败立即抛错（带具体原因），不让上层兜底吞成"未能复制出有效 Cookies"。
    # 其余 sqlite 失败可降级（best-effort），登录态不靠它们。源不存在视为正常（不同 Chrome 版本路径差异）。
    critical_sqlite = {"Cookies", "Login Data"}
    for name in profile_files:
        src = source_profile_dir / name
        dst = runner_profile_dir / name
        if name in sqlite_files:
            ok, reason = _copy_sqlite_best_effort(src, dst)
            if not ok and src.exists() and name in critical_sqlite:
                raise RuntimeError(f"复制关键登录态文件失败 {src}: {reason}")
        else:
            _copy_path(src, dst)

    for name in profile_dirs:
        src = source_profile_dir / name
        dst = runner_profile_dir / name
        if name == "Network":
            dst.mkdir(parents=True, exist_ok=True)
            src_cookies = src / "Cookies"
            ok, reason = _copy_sqlite_best_effort(src_cookies, dst / "Cookies")
            if not ok and src_cookies.exists():
                raise RuntimeError(f"复制 Network/Cookies 失败 {src_cookies}: {reason}")
        else:
            _copy_path(src, dst)

    _ensure_minimal_preferences(runner_profile_dir)
    _strip_google_identity(runner_dir, runner_profile_dir)
    _strip_google_cookies(runner_profile_dir)

    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        _safe_unlink(runner_dir / lock_name)
        _safe_unlink(runner_profile_dir / lock_name)

    if not is_runner_profile_ready(runner_profile_dir):
        raise RuntimeError("专用浏览器 profile 初始化失败，未能复制出有效 Cookies")

    # 记录重建时间：should_rebuild_runner_profile 依赖 meta 判断 3 天内免重建。
    # 此前 _write_runner_meta 从未被调用，副本永远无 meta → 每次抓取都强制
    # 关用户 Chrome 重建副本（骚扰 + 放大关闭失败窗口）
    _write_runner_meta(
        runner_dir,
        profile_name=profile_name,
        source_profile_dir=source_profile_dir,
        rebuilt_at=_now_ts(),
    )
    _write_rebuild_clock()


def _strip_google_account_state(data: dict) -> None:
    """就地删除一份 prefs JSON 中所有"已登录 Google 账号"状态。"""
    for key in ("account_info", "signin", "google.services"):
        data.pop(key, None)


def _strip_google_identity(runner_dir: Path, runner_profile_dir: Path) -> None:
    """从副本中剥离 Google 账号身份，只保留 cookie 解密所需的 os_crypt 密钥。

    副本若携带 account_info / account_token_service，会以同一 Google 账号 +
    同一设备注册身份（Local State 内含 device_id）联网刷新令牌；Google 侧
    检测到"同设备身份异常双活"会吊销会话，导致用户常用 Chrome 被要求重新登录。

    注意必须连 Secure Preferences 一起剥：Chrome 对 signin/google.services 用
    HMAC 保护存储在 Secure Preferences，运行时会从那里把账号状态恢复进
    Preferences——只剥普通 Preferences 时，副本 Chrome 仍以"已登录 profile"
    自居向 Google 发起账号校验（实测出现 account_preview fetch 请求），照样
    触发风控。删除受保护键后 MAC 不匹配，Chrome 会将其重置为默认（未登录）。
    """
    ls_path = runner_dir / "Local State"
    if ls_path.exists():
        try:
            data = json.loads(ls_path.read_text(encoding="utf-8"))
            for key in ("account_info", "account_token_service"):
                data.pop(key, None)
            ls_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError) as exc:
            print(f"[WARN] 剥离副本 Google 身份失败 (Local State): {exc}")

    for prefs_path in (runner_profile_dir / "Preferences", runner_profile_dir / "Secure Preferences"):
        if not prefs_path.exists():
            continue
        try:
            data = json.loads(prefs_path.read_text(encoding="utf-8"))
            _strip_google_account_state(data)
            # 同步删除被删键的 MAC，避免留下"值没了 MAC 还在"的悬挂校验
            macs = data.get("protection", {}).get("macs")
            if isinstance(macs, dict):
                for key in ("account_info", "signin", "google.services"):
                    macs.pop(key, None)
            prefs_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError) as exc:
            print(f"[WARN] 剥离副本 Google 身份失败 ({prefs_path.name}): {exc}")


def _strip_google_cookies(runner_profile_dir: Path) -> None:
    """删除副本中 Google 系域的 cookie（含 SID/HSID/SAPISID 等登录会话）。

    仅剥离身份标识（_strip_google_identity）不够：副本仍携带真实 Google
    会话 cookie 从第二个 profile 路径联网，Google 风控按"同会话异常双活"
    吊销会话，用户常用 Chrome 依旧被要求重登。抓取目标不依赖 Google 登录态，
    让副本彻底以未登录身份访问。best-effort：删除失败仅告警，不阻断抓取。
    """
    delete_sql = "DELETE FROM cookies WHERE host_key LIKE ? OR host_key LIKE ?"
    for cookies_path in (
        runner_profile_dir / "Network" / "Cookies",
        runner_profile_dir / "Cookies",
    ):
        if not cookies_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(cookies_path))
            try:
                conn.execute(delete_sql, ("%google%", "%youtube%"))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(f"[WARN] 剥离副本 Google cookie 失败 ({cookies_path}): {exc}")


def rebuild_runner_profile(
    source_profile_dir: Path,
    runner_dir: Path,
    profile_name: str,
    *,
    root_files: tuple[str, ...] = DEFAULT_ROOT_FILES_TO_COPY,
    profile_files: tuple[str, ...] = DEFAULT_PROFILE_FILES_TO_COPY,
    profile_dirs: tuple[str, ...] = DEFAULT_PROFILE_DIRS_TO_COPY,
) -> None:
    # 关 Chrome 已下沉到 prepare_runner_profile，此处不再重复
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

    直接用 launch_persistent_context 挂载本机 Chrome Default profile（不复制），
    保证登录态（Cookies）原生有效。抓取期间 Chrome 必须关闭（profile 锁）。
    一次 fetch 周期内 start() 起一次 Chrome，各站点 acquire_page() 拿新页面，
    最后 shutdown() 关闭浏览器。
    """

    def __init__(
        self,
        *,
        source_profile_dir: Path | None = None,
        runner_dir: Path = SHARED_RUNNER_DIR,
        debug_port: int = SHARED_RUNNER_DEBUG_PORT,
        profile_name: str = SHARED_RUNNER_PROFILE_NAME,
        rebuild_interval: int = DEFAULT_REBUILD_INTERVAL_SECONDS,
        headless: bool = True,
        extra_args: list[str] | None = None,
    ) -> None:
        # source_profile_dir/runner_dir/debug_port 保留签名兼容旧调用方，
        # 直接挂载模式下不再使用（直接挂 CHROME_USER_DATA）。
        self.source_profile_dir = source_profile_dir
        self.runner_dir = runner_dir
        self.debug_port = debug_port
        self.profile_name = profile_name
        self.rebuild_interval = rebuild_interval
        self.headless = headless
        self.extra_args = extra_args
        self._playwright = None
        self._context = None
        self._started = False
        # 记录 start 前用户 Chrome 是否在跑：shutdown 时据此决定是否重开
        self._chrome_was_running = False

    def start(self) -> None:
        if self._started:
            return
        # 挂载 profile 必须先关闭 Chrome（profile 锁）
        self._chrome_was_running = is_chrome_running()
        if not kill_chrome_gracefully():
            raise RuntimeError("Chrome 正在运行且无法关闭，无法挂载 profile")
        if not _ensure_all_chrome_processes_stopped():
            if not _force_kill_chrome_powershell():
                raise RuntimeError("Chrome 进程未完全退出，无法挂载 profile")

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--no-first-run",
            "--disable-popup-blocking",
        ]
        if self.extra_args:
            args.extend(self.extra_args)
        # source_profile_dir 为要挂载的独立 profile 目录；缺省回退到系统 Default profile
        profile_dir = self.source_profile_dir or (CHROME_USER_DATA / "Default")
        _, chrome_executable = _resolve_default_browser_executable()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=chrome_executable,
            headless=self.headless,
            args=args,
            ignore_default_args=["--enable-automation"],
        )
        self._started = True

    def acquire_page(self):
        if not self._started or self._context is None:
            raise RuntimeError("SharedRunnerSession 未启动，请先调用 start()")
        return self._context.new_page()

    def acquire_page_by_url(self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 30000):
        """优先复用已打开匹配域名 tab，否则新开 tab 并导航到 url。

        供蓝宝书等需要复用特定 tab 的站点使用。
        """
        if not self._started or self._context is None:
            raise RuntimeError("SharedRunnerSession 未启动，请先调用 start()")
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        for candidate in self._context.pages:
            try:
                candidate_host = urlparse(str(candidate.url or "")).hostname or ""
            except Exception:
                candidate_host = ""
            if host and candidate_host == host:
                return candidate
        page = self._context.new_page()
        page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        return page

    def restart(self) -> None:
        """登录态失效时重建：关掉再重启浏览器。restart 中途的 shutdown 不重开 Chrome。"""
        self.shutdown(_reopen_chrome=False)
        self.start()

    def shutdown(self, *, _reopen_chrome: bool = True) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._started = False
        # 抓取前用户 Chrome 在跑、且本次 start 关过它 → 抓取完重开
        if _reopen_chrome and self._chrome_was_running:
            _reopen_user_chrome()
            self._chrome_was_running = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()
        return False


class CopyRunnerSession:
    """复制本机 Chrome Default profile 到副本，再 CDP 抓取。

    供蓝宝书等单设备登录限制的站点使用：不在副本里登录，而是复制你常用
    Chrome 的登录态，副本独立运行不影响你常用浏览器。
    """

    def __init__(
        self,
        *,
        runner_dir: Path,
        debug_port: int,
        source_profile_dir: Path | None = None,
        start_url: str = "about:blank",
        headless: bool = True,
        extra_args: list[str] | None = None,
        rebuild_interval: int = DEFAULT_REBUILD_INTERVAL_SECONDS,
    ) -> None:
        self.runner_dir = runner_dir
        self.debug_port = debug_port
        self.source_profile_dir = source_profile_dir or (CHROME_USER_DATA / "Default")
        self.start_url = start_url
        self.headless = headless
        self.extra_args = extra_args
        self.rebuild_interval = rebuild_interval
        self._playwright = None
        self._browser = None
        self._started = False
        # 记录 start 前用户 Chrome 是否在跑：shutdown 时据此决定是否重开
        self._chrome_was_running = False

    def start(self) -> None:
        if self._started:
            return
        # ensure_debug_browser 可能关掉用户 Chrome 来复制 profile，先记下状态供 shutdown 重开
        self._chrome_was_running = is_chrome_running()
        ensure_debug_browser(
            self.runner_dir,
            "Default",
            self.debug_port,
            self.start_url,
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
            raise RuntimeError("CopyRunnerSession 未启动，请先调用 start()")
        context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        return context.new_page()

    def acquire_page_by_url(self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 30000):
        if not self._started or self._browser is None:
            raise RuntimeError("CopyRunnerSession 未启动，请先调用 start()")
        context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        for candidate in context.pages:
            try:
                candidate_host = urlparse(str(candidate.url or "")).hostname or ""
            except Exception:
                candidate_host = ""
            if host and candidate_host == host:
                return candidate
        page = context.new_page()
        page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        return page

    def restart(self) -> None:
        # 登录态失效时：先删副本强制重建，否则 ensure_debug_browser 的 24h rebuild
        # 缓存会复用失效旧副本，导致"常用 Chrome 重登后"仍抓不到新登录态。
        # 复制的源是同一常用 Chrome Default profile，删除副本不丢任何登录态。
        _remove_runner_dir(self.runner_dir)
        # restart 中途的 shutdown 不重开 Chrome（马上要再 start），否则会跟新 start 抢 profile 锁
        self.shutdown(_reopen_chrome=False)
        self.start()

    def shutdown(self, *, _reopen_chrome: bool = True) -> None:
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
        # 抓取前用户 Chrome 在跑、且本次 start 关过它 → 抓取完重开
        if _reopen_chrome and self._chrome_was_running:
            _reopen_user_chrome()
            self._chrome_was_running = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()
        return False



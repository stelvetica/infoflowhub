from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[3]
RUNTIME_DIR = BASE_DIR / "runtime"
BROWSER_PROFILES_DIR = RUNTIME_DIR / "browser_profiles"

# 方式1：独立登录 profile（共享）。抖音/小黑盒/X/YouTube 共用，cookie 按域名隔离。
# login_profiles.py 拉起浏览器扫码登录。
AUTH_PROFILE_DIR = BROWSER_PROFILES_DIR / "auth"

# 方式2：copy profile。蓝宝书专用——复制本机 Chrome Default profile 登录态到副本，
# 不在副本登录，避免蓝宝书单设备登录限制与你常用浏览器互踢。
ALPHAPAI_RUNNER_DIR = BROWSER_PROFILES_DIR / "alphapai-runner"


def _default_chrome_profile_dir() -> Path:
    from connectors._shared.common import CHROME_USER_DATA
    return CHROME_USER_DATA / "Default"


def _existing_state_files(base_dir: Path) -> list[Path]:
    return [
        path
        for path in (
            base_dir / "Cookies",
            base_dir / "Network" / "Cookies",
            base_dir / "Preferences",
            base_dir / "Local State",
        )
        if path.exists()
    ]


def _validate_profile_dir(base_dir: Path, requirement: str, hint: str) -> dict[str, object]:
    if not base_dir.exists():
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": f"{requirement}，当前未找到：{base_dir}",
        }
    if not _existing_state_files(base_dir):
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "缺少会话",
            "hint": hint,
        }
    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用",
        "hint": hint,
    }


def _validate_auth_profile_cookie(host_keyword: str, label: str) -> dict[str, object]:
    """检查共享 auth profile 里是否含某域名 cookie（判断是否已登录该站点）。"""
    cookies = AUTH_PROFILE_DIR / "Default" / "Network" / "Cookies"
    if not cookies.exists():
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": f"请运行 login_profiles.py {label} 扫码登录（登录态存入共享 auth profile）。",
        }
    try:
        import sqlite3

        conn = sqlite3.connect(str(cookies))
        count = conn.execute(
            "SELECT COUNT(*) FROM cookies WHERE host_key LIKE ?", (f"%{host_keyword}%",)
        ).fetchone()[0]
        conn.close()
    except Exception:
        return {"is_available": False, "status_level": "warn", "status_text": "无法读取", "hint": "auth profile 读取失败"}
    if count <= 0:
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": f"请运行 login_profiles.py {label} 扫码登录。",
        }
    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用",
        "hint": "登录态存于共享 auth profile，cookie 按域名隔离。失效则重新登录。",
    }


def validate_douyin_auth() -> dict[str, object]:
    return _validate_auth_profile_cookie("douyin", "douyin")


def validate_x_auth() -> dict[str, object]:
    return _validate_auth_profile_cookie("x.com", "x")


def validate_xiaoheihe_auth() -> dict[str, object]:
    return _validate_auth_profile_cookie("xiaoheihe", "xiaoheihe")


def validate_youtube_auth() -> dict[str, object]:
    return _validate_auth_profile_cookie("youtube", "youtube")


def validate_alphapai_auth() -> dict[str, object]:
    """蓝宝书：检查常用 Chrome Default profile 是否登了蓝宝书（copy 来源）。"""
    cookies = _default_chrome_profile_dir() / "Network" / "Cookies"
    if not cookies.exists():
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": "请先在常用 Chrome 中登录蓝宝书（alphapai-web.rabyte.cn），抓取时自动复制登录态。",
        }
    try:
        import sqlite3

        conn = sqlite3.connect(str(cookies))
        count = conn.execute(
            "SELECT COUNT(*) FROM cookies WHERE host_key LIKE ?", ("%rabyte%",)
        ).fetchone()[0]
        conn.close()
    except Exception:
        return {"is_available": False, "status_level": "warn", "status_text": "无法读取", "hint": "常用 Chrome 占用中，关 Chrome 后重试"}
    if count <= 0:
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": "请在常用 Chrome 中登录蓝宝书（alphapai-web.rabyte.cn），抓取时自动复制。",
        }
    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用",
        "hint": "蓝宝书登录态从常用 Chrome 复制（单设备限制，不在副本登录）。抓取前需关 Chrome。",
    }


def get_context_path(auth_key: str) -> Path:
    mapping = {
        "douyin_shared": AUTH_PROFILE_DIR,
        "x_profile2": AUTH_PROFILE_DIR,
        "xiaoheihe_shared": AUTH_PROFILE_DIR,
        "youtube_main": AUTH_PROFILE_DIR,
        "alphapai_main": _default_chrome_profile_dir(),
    }
    if auth_key not in mapping:
        raise KeyError(f"未注册的 profile 登录态: {auth_key}")
    return mapping[auth_key]

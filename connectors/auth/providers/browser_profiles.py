from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[3]
RUNTIME_DIR = BASE_DIR / "runtime"
BROWSER_PROFILES_DIR = RUNTIME_DIR / "browser_profiles"

# 独立专用登录 profile（每个站点一个，避开 Chrome 主 profile 的 cookie 加密绑定）
DOUYIN_AUTH_PROFILE_DIR = BROWSER_PROFILES_DIR / "douyin-auth"
X_AUTH_PROFILE_DIR = BROWSER_PROFILES_DIR / "x-auth"
YOUTUBE_AUTH_PROFILE_DIR = BROWSER_PROFILES_DIR / "youtube-auth"
XIAOHEIHE_AUTH_PROFILE_DIR = BROWSER_PROFILES_DIR / "xiaoheihe-auth"
ALPHAPAI_AUTH_PROFILE_DIR = BROWSER_PROFILES_DIR / "alphapai-auth"

# 旧路径（兼容）
DOUYIN_PROFILE_DIR = BROWSER_PROFILES_DIR / "douyin-shared"
X_PROFILE_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
XIAOHEIHE_PROFILE_DIR = X_PROFILE_DIR
ALPHAPAI_PROFILE_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
YOUTUBE_PROFILE_DIR = ALPHAPAI_PROFILE_DIR


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


def validate_douyin_auth() -> dict[str, object]:
    return _validate_profile_dir(
        DOUYIN_PROFILE_DIR,
        "抖音共享登录态目录存在",
        "复用现有抖音登录态；若失效，重新执行抖音登录脚本即可恢复。",
    )


def validate_x_auth() -> dict[str, object]:
    return _validate_profile_dir(
        X_PROFILE_DIR,
        "X 平台共享登录态依赖本机 Chrome Default Profile",
        "请先在本机 Chrome 的 Default 中登录 x.com，并确认时间线可正常加载。",
    )


def validate_xiaoheihe_auth() -> dict[str, object]:
    return _validate_profile_dir(
        XIAOHEIHE_PROFILE_DIR,
        "小黑盒共享登录态依赖本机 Chrome Default Profile",
        "请先在本机 Chrome 的 Default 中登录 xiaoheihe.cn，确认收藏页可正常访问。",
    )


def validate_alphapai_auth() -> dict[str, object]:
    return _validate_profile_dir(
        ALPHAPAI_PROFILE_DIR,
        "Alpha派共享登录态依赖系统 Chrome Default Profile",
        "请先在本机 Chrome 中登录 alphapai-web.rabyte.cn，确认蓝宝书页面可正常访问。抓取前请关闭 Chrome。",
    )


def validate_youtube_auth() -> dict[str, object]:
    return _validate_profile_dir(
        YOUTUBE_PROFILE_DIR,
        "YouTube 共享登录态依赖本机 Chrome Default Profile",
        "可选：在本机 Chrome Default 中登录 YouTube，可降低风控概率。",
    )


def get_context_path(auth_key: str) -> Path:
    mapping = {
        "douyin_shared": DOUYIN_PROFILE_DIR,
        "x_profile2": X_PROFILE_DIR,
        "xiaoheihe_shared": XIAOHEIHE_PROFILE_DIR,
        "alphapai_main": ALPHAPAI_PROFILE_DIR,
        "youtube_main": YOUTUBE_PROFILE_DIR,
    }
    if auth_key not in mapping:
        raise KeyError(f"未注册的 profile 登录态: {auth_key}")
    return mapping[auth_key]

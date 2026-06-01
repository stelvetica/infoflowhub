from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[3]
RUNTIME_DIR = BASE_DIR / "runtime"
BROWSER_PROFILES_DIR = RUNTIME_DIR / "browser_profiles"
DOUYIN_PROFILE_DIR = BROWSER_PROFILES_DIR / "douyin-shared"
WEIBO_PROFILE_DIR = BROWSER_PROFILES_DIR / "weibo-shared"
X_PROFILE_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Profile 2"
XIAOHEIHE_PROFILE_DIR = BROWSER_PROFILES_DIR / "xiaoheihe-shared"


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
        "X 平台共享登录态依赖本机 Chrome Profile 2",
        "请先在本机 Chrome 的 Profile 2 中登录 x.com，并确认时间线可正常加载。",
    )


def validate_weibo_auth() -> dict[str, object]:
    return _validate_profile_dir(
        WEIBO_PROFILE_DIR,
        "微博共享登录态目录存在",
        "请先执行微博登录脚本，完成一次真人登录。",
    )


def validate_xiaoheihe_auth() -> dict[str, object]:
    return _validate_profile_dir(
        XIAOHEIHE_PROFILE_DIR,
        "小黑盒共享登录态目录存在",
        "请先执行小黑盒登录脚本，完成一次真人登录。",
    )


def get_context_path(auth_key: str) -> Path:
    mapping = {
        "douyin_shared": DOUYIN_PROFILE_DIR,
        "x_profile2": X_PROFILE_DIR,
        "weibo_shared": WEIBO_PROFILE_DIR,
        "xiaoheihe_shared": XIAOHEIHE_PROFILE_DIR,
    }
    if auth_key not in mapping:
        raise KeyError(f"未注册的 profile 登录态: {auth_key}")
    return mapping[auth_key]

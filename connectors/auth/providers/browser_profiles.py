from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[3]
RUNTIME_DIR = BASE_DIR / "runtime"
BROWSER_PROFILES_DIR = RUNTIME_DIR / "browser_profiles"

# 方式1：独立登录 profile（共享）。无法复用日常 Chrome 会话的站点用，
# 由登录脚本/人工扫码维护；当前抓取链路不再读取它。
AUTH_PROFILE_DIR = BROWSER_PROFILES_DIR / "auth"


def _default_chrome_profile_dir() -> Path:
    from connectors._shared.common import CHROME_USER_DATA
    return CHROME_USER_DATA / "Default"


def _default_chrome_cookies() -> Path:
    """常用 Chrome Default profile 的 Cookies 文件（copy 登录态来源）。"""
    return _default_chrome_profile_dir() / "Network" / "Cookies"


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


def _cookies_db_readable(path: Path) -> bool:
    """Chrome 运行时会排他锁定 profile 的 Cookies SQLite，读打开会失败。

    用 O_RDONLY 探一次(瞬时，被锁立即失败)，避免让 sqlite3 对一个注定失败的
    锁定文件死等约 1.45s(实测 timeout 设再小也无济于事)。返回 False 即视为
    被 Chrome 独占，直接走"未精确校验，按可用处理"的降级分支。
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        os.close(fd)
        return True
    except OSError:
        return False


def _validate_auth_profile_cookie(
    host_keyword: str,
    label: str,
    *,
    required_cookies: tuple[str, ...] = (),
) -> dict[str, object]:
    """检查常用 Chrome Default profile 里是否含某域名 cookie（判定已登录该站点）。

    抖音/X/小黑盒/YouTube 订阅与收藏现已统一走 bsk 后端，直接复用日常 Chrome
    的实时登录会话（不复制副本、不导出 token），故校验来源是常用 Chrome Default
    （同 validate_alphapai_auth）。required_cookies 非空时，要求这些 cookie 名
    （任一存在即可）在匹配域名下存在才算已登录--避免被追踪/指纹类 cookie
    （GA、百度统计、设备指纹等）蒙混。
    """
    cookies = _default_chrome_cookies()
    if not cookies.exists():
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": f"请先在常用 Chrome 中登录 {label}；抓取走 bsk，复用日常 Chrome 登录会话。",
        }
    # Chrome 运行时锁定 Cookies，读打不开；此时挖不到登录态信息、也降级为"可用"。
    if not _cookies_db_readable(cookies):
        return {"is_available": True, "status_level": "ok", "status_text": "可用", "hint": "常用 Chrome 运行中，未精确校验登录态；抓取走 bsk 复用其实时会话。"}
    try:
        import sqlite3

        conn = sqlite3.connect(str(cookies))
        if required_cookies:
            placeholders = ", ".join("?" for _ in required_cookies)
            auth_count = conn.execute(
                f"SELECT COUNT(*) FROM cookies WHERE host_key LIKE ? AND name IN ({placeholders})",
                (f"%{host_keyword}%", *required_cookies),
            ).fetchone()[0]
        else:
            auth_count = conn.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE ?", (f"%{host_keyword}%",)
            ).fetchone()[0]
        conn.close()
    except Exception:
        # Chrome 运行时会排他锁定 Default profile 的 Cookies SQLite，无法精确校验。
        # 抓取走 bsk 复用其实时会话，不受此锁影响，且有失效检测兜底；故读不了时
        # 降级为"可用"——profile 存在即视为可用。
        return {"is_available": True, "status_level": "ok", "status_text": "可用", "hint": "常用 Chrome 运行中，未精确校验登录态；抓取走 bsk 复用其实时会话。"}
    if auth_count <= 0:
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "未登录",
            "hint": f"请先在常用 Chrome 中登录 {label}；抓取走 bsk，复用日常 Chrome 登录会话。如已登录仍显示未登录，请确认已关闭 Chrome ABE 并重启。",
        }
    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用",
        "hint": "登录态来自日常 Chrome 会话；抓取走 bsk 复用其实时登录态，不导出 token。",
    }


def validate_douyin_auth() -> dict[str, object]:
    return _validate_auth_profile_cookie("douyin", "douyin")


def validate_x_auth() -> dict[str, object]:
    return _validate_auth_profile_cookie("x.com", "x")


def validate_xiaoheihe_auth() -> dict[str, object]:
    # heybox_id 是小黑盒前端判断 isLogin 的唯一标识 cookie（app.js:
    # if(userinfo.heybox_id) isLogin=true; else 读 cookie heybox_id）。
    # 按域名计数会被 GA/百度统计/设备指纹等追踪 cookie 蒙混，必须精确校验。
    return _validate_auth_profile_cookie("xiaoheihe", "xiaoheihe", required_cookies=("heybox_id",))


def validate_youtube_auth() -> dict[str, object]:
    return _validate_auth_profile_cookie("youtube", "youtube")


def validate_jiuyangongshe_auth() -> dict[str, object]:
    """韭研公社：用户主页 SSR 渲染，未登录即可抓取，无需登录态资产。"""
    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "无需登录",
        "hint": "用户主页公开可读，纯 HTTP 抓取，无需登录态。",
    }


def validate_alphapai_auth() -> dict[str, object]:
    """Alpha派：抓取只走 bsk 后端，复用本机日常 Chrome 登录会话。

    只检查日常 Chrome 是否含 Alpha派域名会话这一**非敏感**状态：只做 COUNT，
    绝不读取、返回或打印任何 cookie 名或值。Chrome 运行时其 Cookies SQLite 被
    排他锁定，读不到就降级为「可用」——bsk 直接复用其实时会话，无需复制副本，
    且有失效检测兜底；状态检查失败时只回可读状态文案，不外泄任何敏感信息。
    """
    cookies = _default_chrome_profile_dir() / "Network" / "Cookies"
    if not cookies.exists():
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "需在 Chrome 登录",
            "hint": "请在日常 Chrome 登录 Alpha派。",
        }
    # Chrome 运行时锁定 Cookies，直接降级为可用（bsk 复用实时会话），避免 sqlite 死等。
    if not _cookies_db_readable(cookies):
        return {
            "is_available": True,
            "status_level": "ok",
            "status_text": "可用",
            "hint": "日常 Chrome 运行中，复用其实时登录会话（bsk 后端）。",
        }
    try:
        import sqlite3

        conn = sqlite3.connect(str(cookies))
        count = conn.execute(
            "SELECT COUNT(*) FROM cookies WHERE host_key LIKE ?", ("%rabyte%",)
        ).fetchone()[0]
        conn.close()
    except Exception:
        # 读取失败（占用/损坏）：只给可读状态，不暴露异常细节或 cookie 信息。
        return {
            "is_available": True,
            "status_level": "ok",
            "status_text": "可用",
            "hint": "日常 Chrome 运行中，复用其实时登录会话（bsk 后端）。",
        }
    if count <= 0:
        return {
            "is_available": False,
            "status_level": "warn",
            "status_text": "需在 Chrome 登录",
            "hint": "请在日常 Chrome 登录 Alpha派。",
        }
    return {
        "is_available": True,
        "status_level": "ok",
        "status_text": "可用",
        "hint": "复用日常 Chrome 登录会话（bsk 后端）；copy 后端已禁用，失败时按「最近错误」排查并在日常 Chrome 重新登录。",
    }


def get_context_path(auth_key: str) -> Path:
    mapping = {
        "douyin_shared": _default_chrome_profile_dir(),
        "x_profile2": _default_chrome_profile_dir(),
        "xiaoheihe_shared": _default_chrome_profile_dir(),
        "youtube_main": _default_chrome_profile_dir(),
        "alphapai_main": _default_chrome_profile_dir(),
    }
    if auth_key not in mapping:
        raise KeyError(f"未注册的 profile 登录态: {auth_key}")
    return mapping[auth_key]

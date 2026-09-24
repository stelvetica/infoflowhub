from __future__ import annotations

from pathlib import Path

from connectors.auth.models import AuthDescriptor, AuthRegistration
from connectors.auth.providers import (
    get_bilibili_headers,
    get_context_path,
    validate_alphapai_auth,
    validate_bilibili_auth,
    validate_douyin_auth,
    validate_jiuyangongshe_auth,
    validate_x_auth,
    validate_xiaoheihe_auth,
    validate_youtube_auth,
)


AUTH_REGISTRY: dict[str, AuthRegistration] = {
    # 方式1：bsk 后端复用日常 Chrome 登录会话（失败不回退，需在源表显式用 copy 重试）
    "douyin_shared": AuthRegistration(
        auth_key="douyin_shared",
        platform="douyin",
        auth_mode="browser_profile",
        storage_ref="复用本机 Chrome 会话，不导出 token",
        renew_strategy="login_daily_chrome",
        display_name="抖音",
        description="订阅抖音主页 + laterhub 抖音收藏。抓取走 bsk，复用日常 Chrome 登录会话。",
        login_method="日常 Chrome 登录会话",
        renew_label="请在日常 Chrome 登录抖音",
    ),
    "x_profile2": AuthRegistration(
        auth_key="x_profile2",
        platform="x",
        auth_mode="browser_profile",
        storage_ref="复用本机 Chrome 会话，不导出 token",
        renew_strategy="login_daily_chrome",
        display_name="X (Twitter)",
        description="订阅 MacroMargin 时间线。抓取走 bsk，复用日常 Chrome 登录会话。",
        login_method="日常 Chrome 登录会话",
        renew_label="请在日常 Chrome 登录 X",
    ),
    "xiaoheihe_shared": AuthRegistration(
        auth_key="xiaoheihe_shared",
        platform="xiaoheihe",
        auth_mode="browser_profile",
        storage_ref="复用本机 Chrome 会话，不导出 token",
        renew_strategy="login_daily_chrome",
        display_name="小黑盒",
        description="laterhub 小黑盒收藏。抓取走 bsk，复用日常 Chrome 登录会话。",
        login_method="日常 Chrome 登录会话",
        renew_label="请在日常 Chrome 登录小黑盒",
    ),
    "youtube_main": AuthRegistration(
        auth_key="youtube_main",
        platform="youtube",
        auth_mode="browser_profile",
        storage_ref="复用本机 Chrome 会话，不导出 token",
        renew_strategy="login_daily_chrome",
        display_name="YouTube",
        description="订阅 YouTube 频道。公开内容无需登录；抓取走 bsk 复用日常 Chrome 登录会话。",
        login_method="日常 Chrome 登录会话",
        renew_label="请在日常 Chrome 登录 YouTube",
    ),
    "jiuyangongshe_main": AuthRegistration(
        auth_key="jiuyangongshe_main",
        platform="jiuyangongshe",
        auth_mode="none",
        storage_ref="无需登录态",
        renew_strategy="none",
        display_name="韭研公社",
        description="用户主页公开可读，纯 HTTP 抓取，无需登录态。",
        login_method="无需登录",
        renew_label="",
    ),
    # 方式2：同上，蓝宝书/公众号两条源共用
    "alphapai_main": AuthRegistration(
        auth_key="alphapai_main",
        platform="alphapai",
        auth_mode="browser_profile",
        storage_ref="复用本机 Chrome 会话，不导出 token",
        renew_strategy="login_daily_chrome",
        display_name="Alpha派",
        description="抓取后端 bsk，复用日常 Chrome 登录会话；bsk 失败不回退，可在源表手动用 copy 重试。",
        login_method="日常 Chrome 登录会话",
        renew_label="请在日常 Chrome 登录 Alpha派",
    ),
    # 方式3：B站混合——订阅动态走 bsk 页内调 API（复用日常 Chrome 登录态，不走 HTTP）；
    # 稍后看仍走 HTTP，读本地认证文件 bilibili_main.json 里的 SESSDATA/bili_jct/DedeUserID。
    # 该文件由「同步登录态」从常用 Chrome 导入，故保留同步按钮。
    "bilibili_main": AuthRegistration(
        auth_key="bilibili_main",
        platform="bilibili",
        auth_mode="browser_profile",
        storage_ref="本地认证文件 bilibili_main.json（稍后看）",
        renew_strategy="scan_qr_sync",
        display_name="B站",
        description="订阅 B站动态已改走 bsk（页内调 API，复用日常 Chrome 登录态）；稍后看仍用本地认证文件。",
        login_method="日常 Chrome 登录会话",
        renew_label="在常用 Chrome 登录 B站后点「同步登录态」",
    ),
}


def resolve_auth(auth_key: str) -> AuthRegistration:
    key = str(auth_key or "").strip()
    if key not in AUTH_REGISTRY:
        raise KeyError(f"未注册的认证资产: {key}")
    return AUTH_REGISTRY[key]


def validate_auth(auth_key: str) -> AuthDescriptor:
    registration = resolve_auth(auth_key)
    if auth_key == "douyin_shared":
        status = validate_douyin_auth()
    elif auth_key == "bilibili_main":
        status = validate_bilibili_auth()
    elif auth_key == "x_profile2":
        status = validate_x_auth()
    elif auth_key == "xiaoheihe_shared":
        status = validate_xiaoheihe_auth()
    elif auth_key == "alphapai_main":
        status = validate_alphapai_auth()
    elif auth_key == "youtube_main":
        status = validate_youtube_auth()
    elif auth_key == "jiuyangongshe_main":
        status = validate_jiuyangongshe_auth()
    else:
        status = {"is_available": False, "status_level": "warn", "status_text": "未知", "hint": ""}
    return AuthDescriptor(
        auth_key=registration.auth_key,
        platform=registration.platform,
        auth_mode=registration.auth_mode,
        storage_ref=registration.storage_ref,
        renew_strategy=registration.renew_strategy,
        display_name=registration.display_name,
        description=registration.description,
        status_text=str(status.get("status_text") or ""),
        status_level=str(status.get("status_level") or "warn"),
        hint=str(status.get("hint") or ""),
        is_available=bool(status.get("is_available")),
        is_expired=bool(status.get("is_expired")),
        is_expiring_soon=bool(status.get("is_expiring_soon")),
        remaining_hours=int(status.get("remaining_hours", -1) or -1),
        login_method=registration.login_method,
        nickname=str(status.get("nickname") or ""),
    )


_AUTH_STATUS_CACHE: tuple[float, list[AuthDescriptor]] | None = None
_AUTH_STATUS_CACHE_TTL = 60.0


def list_auth_statuses() -> list[AuthDescriptor]:
    """带 60 秒短时缓存。一次设置页渲染会多次调用，避免重复读 SQLite / Chrome Cookies。"""
    global _AUTH_STATUS_CACHE
    import time as _time

    now = _time.time()
    if _AUTH_STATUS_CACHE is not None and (now - _AUTH_STATUS_CACHE[0]) < _AUTH_STATUS_CACHE_TTL:
        return _AUTH_STATUS_CACHE[1]
    result = [validate_auth(auth_key) for auth_key in AUTH_REGISTRY]
    _AUTH_STATUS_CACHE = (now, result)
    return result


def invalidate_auth_status_cache() -> None:
    """登录/重新扫码后调用，使下次读取刷新缓存。"""
    global _AUTH_STATUS_CACHE
    _AUTH_STATUS_CACHE = None



def get_auth_headers(auth_key: str) -> dict[str, str]:
    if auth_key == "bilibili_main":
        return get_bilibili_headers()
    raise ValueError(f"{auth_key} 不是 cookie/session 类型登录态")


def get_auth_context_path(auth_key: str) -> Path:
    return get_context_path(auth_key)

from __future__ import annotations

from pathlib import Path

from connectors.auth.models import AuthDescriptor, AuthRegistration
from connectors.auth.providers import (
    WECHAT_AUTH_PATH,
    get_bilibili_headers,
    get_context_path,
    get_wechat_status,
    validate_alphapai_auth,
    validate_bilibili_auth,
    validate_douyin_auth,
    validate_x_auth,
    validate_xiaoheihe_auth,
    validate_youtube_auth,
)


AUTH_REGISTRY: dict[str, AuthRegistration] = {
    # 方式1：独立登录 profile（共享 auth profile，扫码登录）
    "douyin_shared": AuthRegistration(
        auth_key="douyin_shared",
        platform="douyin",
        auth_mode="browser_profile",
        storage_ref="共享 auth profile",
        renew_strategy="scan_qr",
        display_name="抖音",
        description="订阅抖音主页 + laterhub 抖音收藏共用。",
        login_method="独立登录扫码",
        renew_label="运行 login_profiles.py douyin",
    ),
    "x_profile2": AuthRegistration(
        auth_key="x_profile2",
        platform="x",
        auth_mode="browser_profile",
        storage_ref="共享 auth profile",
        renew_strategy="scan_qr",
        display_name="X (Twitter)",
        description="订阅 MacroMargin 时间线。",
        login_method="独立登录扫码",
        renew_label="运行 login_profiles.py x",
    ),
    "xiaoheihe_shared": AuthRegistration(
        auth_key="xiaoheihe_shared",
        platform="xiaoheihe",
        auth_mode="browser_profile",
        storage_ref="共享 auth profile",
        renew_strategy="scan_qr",
        display_name="小黑盒",
        description="laterhub 小黑盒收藏。",
        login_method="独立登录扫码",
        renew_label="运行 login_profiles.py xiaoheihe",
    ),
    "youtube_main": AuthRegistration(
        auth_key="youtube_main",
        platform="youtube",
        auth_mode="browser_profile",
        storage_ref="共享 auth profile",
        renew_strategy="scan_qr",
        display_name="YouTube",
        description="订阅 YouTube 频道（公开内容可不登录）。",
        login_method="独立登录扫码",
        renew_label="运行 login_profiles.py youtube",
    ),
    # 方式2：copy profile（蓝宝书单设备限制，复制常用 Chrome 登录态）
    "alphapai_main": AuthRegistration(
        auth_key="alphapai_main",
        platform="alphapai",
        auth_mode="copy_profile",
        storage_ref="复制常用 Chrome Default",
        renew_strategy="copy_default_profile",
        display_name="蓝宝书",
        description="单设备登录限制，复制常用 Chrome 登录态，抓取前关 Chrome。",
        login_method="复制常用浏览器",
        renew_label="在常用 Chrome 登录蓝宝书",
    ),
    # 方式3：cookie 手动填 .env
    "bilibili_main": AuthRegistration(
        auth_key="bilibili_main",
        platform="bilibili",
        auth_mode="cookie_session",
        storage_ref=".env:BILIBILI_COOKIE/SESSDATA",
        renew_strategy="manual_cookie_refresh",
        display_name="B站",
        description="订阅 B站动态 + laterhub 稍后看共用。",
        login_method="手动填 .env cookie",
        renew_label="编辑 .env 配置 BILIBILI_SESSDATA",
    ),
    # 方式4：扫码公众号
    "wechat_mp_main": AuthRegistration(
        auth_key="wechat_mp_main",
        platform="wechat",
        auth_mode="cookie_session",
        storage_ref=str(WECHAT_AUTH_PATH),
        renew_strategy="scan_qr",
        display_name="微信公众号",
        description="9 个公众号文章抓取共用。",
        login_method="扫码公众号后台",
        renew_label="重新扫码",
    ),
}


def resolve_auth(auth_key: str) -> AuthRegistration:
    key = str(auth_key or "").strip()
    if key not in AUTH_REGISTRY:
        raise KeyError(f"未注册的认证资产: {key}")
    return AUTH_REGISTRY[key]


def validate_auth(auth_key: str) -> AuthDescriptor:
    registration = resolve_auth(auth_key)
    if auth_key == "wechat_mp_main":
        status = get_wechat_status()
    elif auth_key == "douyin_shared":
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
    )


def list_auth_statuses() -> list[AuthDescriptor]:
    return [validate_auth(auth_key) for auth_key in AUTH_REGISTRY]


def get_auth_headers(auth_key: str) -> dict[str, str]:
    if auth_key == "bilibili_main":
        return get_bilibili_headers()
    if auth_key == "wechat_mp_main":
        descriptor = validate_auth(auth_key)
        if not descriptor.is_available:
            raise ValueError(descriptor.hint or "微信登录态不可用")
        from connectors.auth.providers import get_wechat_credentials

        credentials = get_wechat_credentials()
        return {"Cookie": str(credentials.get("cookie") or "").strip()}
    raise ValueError(f"{auth_key} 不是 cookie/session 类型登录态")


def get_auth_context_path(auth_key: str) -> Path:
    return get_context_path(auth_key)

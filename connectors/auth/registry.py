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
    validate_weibo_auth,
    validate_x_auth,
    validate_xiaoheihe_auth,
)


AUTH_REGISTRY: dict[str, AuthRegistration] = {
    "wechat_mp_main": AuthRegistration(
        auth_key="wechat_mp_main",
        platform="wechat",
        auth_mode="cookie_session",
        storage_ref=str(WECHAT_AUTH_PATH),
        renew_strategy="scan_qr",
        display_name="微信公众号主账号",
        description="公众号文章抓取统一复用这份登录态。",
    ),
    "douyin_shared": AuthRegistration(
        auth_key="douyin_shared",
        platform="douyin",
        auth_mode="browser_profile",
        storage_ref=str(get_context_path("douyin_shared")),
        renew_strategy="use_system_profile",
        display_name="抖音共享登录态",
        description="later 收藏与订阅抖音源共用同一份浏览器登录态。",
    ),
    "bilibili_main": AuthRegistration(
        auth_key="bilibili_main",
        platform="bilibili",
        auth_mode="cookie_session",
        storage_ref=".env:BILIBILI_COOKIE/BILIBILI_SESSDATA",
        renew_strategy="manual_cookie_refresh",
        display_name="B站主账号",
        description="稍后看与 B 站动态抓取统一复用这份登录态。",
    ),
    "x_profile2": AuthRegistration(
        auth_key="x_profile2",
        platform="x",
        auth_mode="chrome_profile",
        storage_ref=str(get_context_path("x_profile2")),
        renew_strategy="use_system_profile",
        display_name="X 平台共享登录态",
        description="当前复用本机 Chrome Profile 2 登录态。",
    ),
    "weibo_shared": AuthRegistration(
        auth_key="weibo_shared",
        platform="weibo",
        auth_mode="browser_profile",
        storage_ref=str(get_context_path("weibo_shared")),
        renew_strategy="use_system_profile",
        display_name="微博共享登录态",
        description="微博网页抓取统一复用这份浏览器登录态。",
    ),
    "xiaoheihe_shared": AuthRegistration(
        auth_key="xiaoheihe_shared",
        platform="xiaoheihe",
        auth_mode="browser_profile",
        storage_ref=str(get_context_path("xiaoheihe_shared")),
        renew_strategy="use_system_profile",
        display_name="小黑盒共享登录态",
        description="later 收藏夹抓取复用这份浏览器登录态。",
    ),
    "alphapai_main": AuthRegistration(
        auth_key="alphapai_main",
        platform="alphapai",
        auth_mode="chrome_profile",
        storage_ref=str(get_context_path("alphapai_main")),
        renew_strategy="use_system_profile",
        display_name="Alpha派蓝宝书",
        description="复用系统 Chrome Default Profile 登录态。抓取前需关闭 Chrome。",
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
    elif auth_key == "weibo_shared":
        status = validate_weibo_auth()
    elif auth_key == "xiaoheihe_shared":
        status = validate_xiaoheihe_auth()
    elif auth_key == "alphapai_main":
        status = validate_alphapai_auth()
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

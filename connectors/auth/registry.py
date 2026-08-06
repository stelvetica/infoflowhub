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
    validate_jiuyangongshe_auth,
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
        storage_ref="复制常用 Chrome Default",
        renew_strategy="copy_default_profile",
        display_name="抖音",
        description="订阅抖音主页 + laterhub 抖音收藏。抓取时复制常用 Chrome 登录态。",
        login_method="复制常用Chrome登录态",
        renew_label="在常用 Chrome 登录抖音",
    ),
    "x_profile2": AuthRegistration(
        auth_key="x_profile2",
        platform="x",
        auth_mode="browser_profile",
        storage_ref="复制常用 Chrome Default",
        renew_strategy="copy_default_profile",
        display_name="X (Twitter)",
        description="订阅 MacroMargin 时间线。需先在常用 Chrome 登录 X。",
        login_method="复制常用Chrome登录态",
        renew_label="在常用 Chrome 登录 X",
    ),
    "xiaoheihe_shared": AuthRegistration(
        auth_key="xiaoheihe_shared",
        platform="xiaoheihe",
        auth_mode="browser_profile",
        storage_ref="复制常用 Chrome Default",
        renew_strategy="copy_default_profile",
        display_name="小黑盒",
        description="laterhub 小黑盒收藏。抓取时复制常用 Chrome 登录态。",
        login_method="复制常用Chrome登录态",
        renew_label="在常用 Chrome 登录小黑盒",
    ),
    "youtube_main": AuthRegistration(
        auth_key="youtube_main",
        platform="youtube",
        auth_mode="browser_profile",
        storage_ref="复制常用 Chrome Default",
        renew_strategy="copy_default_profile",
        display_name="YouTube",
        description="订阅 YouTube 频道。公开内容可不登录，登录后抓取更稳定。",
        login_method="复制常用Chrome登录态",
        renew_label="在常用 Chrome 登录 YouTube",
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
    # 方式2：copy profile（蓝宝书单设备限制，复制常用 Chrome 登录态）
    "alphapai_main": AuthRegistration(
        auth_key="alphapai_main",
        platform="alphapai",
        auth_mode="copy_profile",
        storage_ref="复制常用 Chrome Default",
        renew_strategy="copy_default_profile",
        display_name="蓝宝书",
        description="单设备登录限制，复制常用 Chrome 登录态，抓取前关 Chrome。需关闭 Chrome ABE。",
        login_method="复制常用Chrome(单设备限制)",
        renew_label="在常用 Chrome 登录蓝宝书",
    ),
    # 方式3：B站特殊——复用登录态拿 cookie，再直接调 API 抓取（不走浏览器）
    # 从常用 Chrome 或共享 profile 读出 SESSDATA/bili_jct/DedeUserID，持久化为
    # 本地认证文件，抓取时用该 cookie 直接请求 api.bilibili.com（HTTP），性能高。
    "bilibili_main": AuthRegistration(
        auth_key="bilibili_main",
        platform="bilibili",
        auth_mode="browser_profile",
        storage_ref="本地认证文件 bilibili_main.json",
        renew_strategy="scan_qr_sync",
        display_name="B站",
        description="订阅 B站动态 + 稍后看。复用登录态取 cookie 后直接调 API 抓取（不走浏览器），比浏览器抓取更快。",
        login_method="复用登录态取Cookie",
        renew_label="在常用 Chrome 登录 B站后点同步",
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
        login_method="扫码登录(公众号后台)",
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
_AUTH_STATUS_CACHE_TTL = 10.0


def list_auth_statuses() -> list[AuthDescriptor]:
    """带 10 秒短时缓存。一次设置页渲染会多次调用，避免重复读 SQLite。"""
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

"""抖音视频能力 connector：直链解析 / 下载合流。

供编排层（video digest）调用，链路：
- resolve_douyin_video_media  用 bsk 打开视频页取直链，四通道依次尝试：
  ① 页面标记（用户脚本 scripts/douyin_media_export.user.js 写入的 play_addr）
  ② bsk 网络缓冲  ③ resource timing  ④ 页面 HTML 的 <source src>
- download_douyin_video       resolve → 立即流式下载 → ffmpeg 合流为 mp4 → ffprobe 读时长

为什么主通道是用户脚本：抖音页面把作品数据放在 React props.videoDetail 与
/aweme/v1/web/aweme/detail/ 响应里，play_addr 是**已合流 mp4**且带多 CDN 备用。但 bsk 的
evaluate 要先过整页基线校验，视频一播放就整页在变、动作被拒（实测长视频 8/8 被拒），
而用户脚本跑在页面自己里不受这道闸门约束，把直链写进 <meta id="ifh-douyin-media">，
本地用 get-html 读（get-html 不需要协作上下文）。

其余通道是兜底：播放器实际发出的 douyinvod 请求（网络缓冲）→ resource timing →
老式页面的 <source src>。直链时效约几分钟，拿到必须立刻下载。

参考实现：https://github.com/WhiteSevs/TamperMonkeyScript 「抖音优化」脚本（同一取数点，
本仓用户脚本按它的网络钩子思路实现）。
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

from connectors._shared.bsk_browser import BskError, BskPage
from connectors._shared.bsk_session import BskRunnerSession

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)
REFERER = "https://www.douyin.com/"
# 播放器发媒体请求需要几秒，导航完成后固定等待
PLAYER_WAIT_MS = 6000

_DOUYIN_VIDEO_URL_RE = re.compile(r"douyin\.com/video/(\d+)")

# 一次 evaluate 拿全：MSE 场景下 video.src 是 blob:，直链只存在于 resource timing 中。
_RESOLVE_JS = r"""
() => {
  const entries = performance.getEntriesByType('resource') || [];
  const media = entries.map(e => e.name).filter(u => u.includes('douyinvod.com') || u.includes('/aweme/v1/play/'));
  return {media: media.slice(0, 8), all_count: media.length};
}
"""

# 兜底通道：播放器没起播时 resource timing 一条媒体请求都没有，但视频页 HTML 的
# <source src> 里本来就带好完整签名直链（实测老视频沿用原生 <video>，非 MSE）。
# 优先按 <source> 取（只可能是当前视频），没有再退回到全页任意 douyinvod 链接。
_HTML_SOURCE_RE = re.compile(r"""<source[^>]+src=["']([^"']*douyinvod\.com[^"']*)["']""")
_HTML_MEDIA_RE = re.compile(r"""https://[^"'<>\s]*douyinvod\.com/[^"'<>\s]*""")

# 用户脚本（scripts/douyin_media_export.user.js）写进页面 head 的直链标记
_MARKER_TAG_RE = re.compile(r"""<meta[^>]*\bid=["']ifh-douyin-media["'][^>]*>""", re.IGNORECASE)
_MARKER_CONTENT_RE = re.compile(r"""\bcontent=["']([^"']*)["']""", re.IGNORECASE)
# 只接受这两个域名的 https 直链：标记来自页面 DOM，属于不可信输入，不能把任意地址交给下载器
_MEDIA_HOST_RE = re.compile(r"^https://([a-z0-9.-]+\.)?(douyinvod\.com|douyin\.com)/", re.IGNORECASE)
# 字幕轨在抖音各家 CDN 上（douyinvod / ixigua / byteimg / douyinpic）；同样按白名单收，
# 标记里的 URL 不可信。
_SUBTITLE_HOST_RE = re.compile(
    r"^https://([a-z0-9.-]+\.)?"
    r"(douyinvod\.com|douyin\.com|douyinpic\.com|ixigua\.com|byteimg\.com|bytegecko\.com|snssdk\.com|bytetos\.com)/",
    re.IGNORECASE,
)


def _marker_entry(page: BskPage, video_id: str) -> dict[str, Any] | None:
    """从用户脚本的 DOM 标记里取出目标视频那一条（没有则 None）。

    取 HTML 不依赖 cooperate 上下文，所以页面在播放也读得到——这正是它优于 evaluate 的地方。
    """
    if not video_id:
        return None
    try:
        text = page.content()
    except BskError:
        return None
    for tag in _MARKER_TAG_RE.findall(text):
        match = _MARKER_CONTENT_RE.search(tag)
        if not match:
            continue
        try:
            data = json.loads(base64.b64decode(html.unescape(match.group(1))).decode("utf-8"))
        except (ValueError, binascii.Error, UnicodeDecodeError):
            continue
        # current 是脚本按 URL 认定的当前视频（不参与淘汰）；entries 是最近几条（含相关推荐）
        candidates = [data.get("current")] + list(data.get("entries") or [])
        for entry in candidates:
            if isinstance(entry, dict) and str(entry.get("id") or "") == video_id:
                return entry
    return None


def _allowed_urls(entry: dict[str, Any], key: str, host_re: re.Pattern[str]) -> list[str]:
    return [url for url in (entry.get(key) or []) if isinstance(url, str) and host_re.match(url)]


def _media_urls_from_marker(page: BskPage, video_id: str) -> list[str]:
    """标记里的播放直链（主通道）；没有目标视频或校验不过时返回空表。"""
    return _allowed_urls(_marker_entry(page, video_id) or {}, "playUrls", _MEDIA_HOST_RE)


def _subtitle_urls_from_marker(page: BskPage, video_id: str) -> list[str]:
    """标记里的作品自带字幕地址（用户脚本 1.1.0 起写入）；没有就返回空表。"""
    return _allowed_urls(_marker_entry(page, video_id) or {}, "subtitleUrls", _SUBTITLE_HOST_RE)


def _split_media_urls(media_urls: list[str]) -> tuple[str | None, str | None]:
    """按流标记分出 (视频, 音频)。

    无 media-video-/media-audio- 标记的是老式页面的单条直链（音视频已合流的 muxed mp4），
    按视频流处理；ffmpeg 转容器即得含音轨的成片。
    """
    video_url = next((u for u in media_urls if "media-video-" in u), None)
    audio_url = next((u for u in media_urls if "media-audio-" in u), None)
    if not video_url and not audio_url:
        video_url = media_urls[0] if media_urls else None
    return video_url, audio_url


def _media_urls_from_network(page: BskPage, *, exclude: set[str] | None = None) -> list[str]:
    """从扩展网络缓冲里取媒体请求（主通道），新请求优先。

    bsk 的 network 是只读命令，不需要 cooperate 上下文：视频页一边播放一边改整页基线，
    evaluate 会被 strict 策略反复拒绝，而缓冲里照样能看到播放器发出的 douyinvod 请求。
    exclude 用于剔除导航前就已在缓冲里的旧链接——宁可回退其它通道，也不拿上一条视频的
    直链冒充本条。
    """
    try:
        entries = page.network_entries()
    except BskError:
        return []
    stale = exclude or set()
    urls: list[str] = []
    # 倒序（sequence 大 = 新）：同一条视频页会重复请求同一路径，取最新签名才不过期。
    for entry in sorted(entries, key=lambda e: e.get("sequence") or 0, reverse=True):
        url = str(entry.get("url") or "")
        if not url or url in stale or url in urls:
            continue
        if "douyinvod.com" not in url and "/aweme/v1/play/" not in url:
            continue
        # 只认真正拿到流的响应；403/失败的请求不算证据
        if entry.get("status") not in (200, 206):
            continue
        urls.append(url)
    return urls


def _media_urls_from_html(page: BskPage) -> list[str]:
    """从当前视频页 HTML 里取直链（兜底用）。HTML 实体与 JSON 转义都还原成真实 URL。

    取 HTML 不依赖 cooperate 上下文，evaluate 被 strict 策略拒绝时仍然可用；
    页面已关闭/取不到时返回空表，由调用方按「无直链」处理。
    """
    try:
        text = page.content()
    except BskError:
        return []
    candidates = [m.group(1) for m in _HTML_SOURCE_RE.finditer(text)]
    if not candidates:
        candidates = [m.group(0) for m in _HTML_MEDIA_RE.finditer(text)]
    urls: list[str] = []
    for raw in candidates:
        url = html.unescape(raw.replace("\\u0026", "&"))
        if url not in urls:
            urls.append(url)
    return urls


@dataclass(slots=True)
class DouyinVideoMedia:
    video_url: str | None  # douyinvod 视频流直链
    audio_url: str | None  # 音频流直链（分离流时非 None；合流场景可能为 None）
    # 作品自带字幕（作者上传或抖音自动生成）的地址；非空时上层可跳过语音转写。
    subtitle_urls: list[str] = field(default_factory=list)


def fetch_douyin_subtitle_text(url: str, *, timeout: float = 30.0) -> str:
    """下载字幕文本（SRT/VTT/JSON 原样返回）；非 200 或空内容抛 RuntimeError。

    字幕 CDN 不需要 Cookie，只认 Referer（实测与播放直链一致）；调用方负责解析格式。
    """
    if not _SUBTITLE_HOST_RE.match(url or ""):
        raise RuntimeError(f"非白名单字幕地址: {url[:120]}")
    response = _build_download_session().get(url, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"字幕下载失败 HTTP {response.status_code}: {url[:120]}")
    text = response.text
    if not text.strip():
        raise RuntimeError(f"字幕内容为空: {url[:120]}")
    return text


def is_douyin_video_url(url: str) -> bool:
    """是否抖音视频页链接（douyin.com/video/<数字>）。"""
    return bool(url) and bool(_DOUYIN_VIDEO_URL_RE.search(url))


def extract_douyin_video_id(url: str) -> str:
    """从链接提取数字视频 ID，解析失败返回 ""。"""
    if not url:
        return ""
    match = _DOUYIN_VIDEO_URL_RE.search(url)
    return match.group(1) if match else ""


def resolve_douyin_video_media(
    url: str,
    *,
    session: BskRunnerSession | None = None,
    timeout: float = 60.0,
) -> DouyinVideoMedia:
    """用 bsk 打开视频页，取音视频直链：先读 resource timing，空则退回页面 HTML。

    session 非 None 时复用调用方会话且不关页（由调用方管理）；None 时自建自毁。
    video_url 为 None（无直链可下）时抛 RuntimeError。
    """
    if not is_douyin_video_url(url):
        raise RuntimeError(f"非抖音视频页链接: {url}")

    own_session = session is None
    if own_session:
        session = BskRunnerSession(start_url="about:blank")
        session.start()

    try:
        page = session.acquire_page()
        media_urls: list[str] = []
        subtitle_urls: list[str] = []
        evaluate_error = ""
        navigated = False
        video_id = extract_douyin_video_id(url)
        # 导航前缓冲里已有的媒体链接：页面这条视频若一个请求都没发，绝不能拿旧链接冒充
        stale_media = set(_media_urls_from_network(page))
        # 播放器发媒体请求的时机不固定，且 bsk 会话冷启动后首个抖音页可能加载不出播放器
        # （实测：独立会话能拿到、连续编排偶发拿不到）——最多两次导航机会，每次 3 轮补等。
        for attempt in range(2):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            except BskError:
                # strict 策略按整页基线校验：视频页加载中基线一直在变，重复导航会被拒。
                # 首跳就失败说明页面没停在目标页，只能报错；二次导航被拒时页面已在目标页，
                # 丢掉整次解析亏了——交给下面的兜底通道。
                if not navigated:
                    raise
                break
            navigated = True
            for index in range(3):
                page.wait_for_timeout(PLAYER_WAIT_MS)
                # 主通道：用户脚本写进 DOM 的 play_addr（已合流、多 CDN）。每轮都回读：
                # 脚本是异步写到标记里的，页面渲染慢时（本地 whisper 占满 CPU 的批量场景）
                # 6 秒时还没写好，只读第一轮会把「来晚的直链」误判成「视频不可下载」。
                entry = _marker_entry(page, video_id) or {}
                media_urls = _allowed_urls(entry, "playUrls", _MEDIA_HOST_RE)
                subtitle_urls = _allowed_urls(entry, "subtitleUrls", _SUBTITLE_HOST_RE) or subtitle_urls
                if media_urls:
                    break
                media_urls = _media_urls_from_network(page, exclude=stale_media)
                if media_urls:
                    break
                try:
                    value = page.evaluate(_RESOLVE_JS)
                except BskError as exc:
                    # strict 策略按整页基线校验：视频页加载/播放中基线一直在变，evaluate
                    # 会被拒（实测连续 8 次 permission_denied）。这不是「视频不可下」。
                    evaluate_error = str(exc)[:200]
                    continue
                # 页内函数没被真正执行时 bsk 会回函数自身/非 dict（实测，函数按值序列化），
                # 静默当「无直链」会把「提取失败」伪装成「视频不可下载」。
                if not isinstance(value, dict):
                    evaluate_error = f"页内提取返回异常类型: {type(value).__name__}"
                    continue
                media_urls = [str(u) for u in value.get("media") or [] if u]
                if media_urls:
                    break
            if media_urls:
                break
        # 三条通道都空（页面没播放、也没渲染 <source>）时再读 HTML 兜一次
        media_urls = media_urls or _media_urls_from_html(page)
        video_url, audio_url = _split_media_urls(media_urls)
        # 播放器可能只发了音频流（实测个别视频如此）——ASR 只需音轨，仅音频也接受
        if not video_url and not audio_url:
            detail = f"（页内提取失败: {evaluate_error}）" if evaluate_error else ""
            raise RuntimeError(
                f"未从抖音视频页提取到可下载直链，页面标记/网络缓冲/resource timing/HTML 均无媒体链接{detail}: {url}"
            )
        return DouyinVideoMedia(
            video_url=str(video_url or audio_url),
            audio_url=str(audio_url) if audio_url else None,
            subtitle_urls=subtitle_urls,
        )
    finally:
        if own_session:
            session.shutdown()


def _build_download_session() -> requests.Session:
    """构造统一下载会话：Chrome UA + Referer，绕过系统代理。"""
    session = requests.Session()
    # 抖音是国内站点，绕过系统代理（本地代理未开时避免 ProxyError）
    session.trust_env = False
    session.headers.update({"User-Agent": USER_AGENT, "Referer": REFERER})
    return session


def _download_stream(session: requests.Session, url: str, dest: Path, *, max_bytes: int, timeout: float) -> int:
    """流式下载到 dest，返回字节数；非 200 或超 max_bytes 抛 RuntimeError。

    抖音 CDN 对大文件裸下载会中途掐断（实测 51MB 流在 12MB 处 IncompleteRead），浏览器
    播放是分片 Range 请求所以无恙——故这里按浏览器行为做断点续传：连接中断时带
    Range: bytes=<已下载>- 重开请求接着下，最多续传 3 次仍未完成才算失败。
    """
    total = 0
    resume_attempts = 0
    while True:
        headers = {"Range": f"bytes={total}-"} if total else {}
        try:
            with session.get(url, timeout=timeout, stream=True, headers=headers) as response:
                if response.status_code not in (200, 206):
                    raise RuntimeError(f"抖音直链下载失败 HTTP {response.status_code}: {url}")
                with dest.open("ab" if total else "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError(f"下载超过 max_bytes 限制({max_bytes})，已中止: {url}")
                        handle.write(chunk)
            break
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as exc:
            # 断点续传：IncompleteRead/连接中断都落在这里；Range 续传从 total 继续
            resume_attempts += 1
            if resume_attempts > 3:
                raise RuntimeError(f"抖音直链下载中断超过 3 次（已下 {total} 字节）: {exc}") from exc
    return total


def _run_ffmpeg(inputs: list[Path], out: Path) -> None:
    """ffmpeg 无损合流/转容器（参数数组，禁 shell 拼接）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg 命令，无法合流抖音音视频流")
    cmd = [ffmpeg, "-y", "-v", "error"]
    for path in inputs:
        cmd += ["-i", str(path)]
    cmd += ["-c", "copy", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(f"ffmpeg 合流失败(exit={result.returncode}): {tail[-1] if tail else '无输出'}")


def _probe_duration_seconds(path: Path) -> int | None:
    """ffprobe 读时长（秒，取整）；失败返回 None（时长仅用于简报展示，不阻断下载）。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        duration = float((json.loads(result.stdout or "{}").get("format") or {}).get("duration"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return int(round(duration))


def download_douyin_video(
    url: str,
    *,
    dest_dir: Path,
    session: BskRunnerSession | None = None,
    max_bytes: int = 2**31,
    timeout: float = 1800.0,
    resolved: dict[str, Any] | None = None,
) -> tuple[Path, int | None]:
    """下载抖音视频到 dest_dir/<视频id>.mp4，返回 (path, duration_seconds)。

    resolve 后立即流式下载（直链时效约几分钟，不能等）；video+audio 都有则 ffmpeg 合流，
    仅 video 则单流转 mp4 容器。任一响应非 200 / 超 max_bytes / ffmpeg 失败：清理半成品并抛
    RuntimeError。ffprobe 失败不抛，duration 返回 None。

    resolved 非 None 时把解析结果（含字幕地址）写回该字典的 "media" 键：调用方
    （video_digest）要拿字幕决定是否跳过转写，而解析只在下载前做一次，不能重复导航。
    """
    media = resolve_douyin_video_media(url, session=session, timeout=min(timeout, 60.0))
    if resolved is not None:
        resolved["media"] = media
    video_id = extract_douyin_video_id(url)
    if not video_id:
        raise RuntimeError(f"无法从链接解析出抖音视频 ID: {url}")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = dest_dir / f"{video_id}.mp4"
    video_path = dest_dir / f"{video_id}.video.mp4"
    audio_path = dest_dir / f"{video_id}.audio.mp4"
    created = [video_path, final_path]

    download_session = _build_download_session()
    try:
        _download_stream(download_session, media.video_url, video_path, max_bytes=max_bytes, timeout=timeout)
        if media.audio_url:
            created.append(audio_path)
            _download_stream(download_session, media.audio_url, audio_path, max_bytes=max_bytes, timeout=timeout)
            _run_ffmpeg([video_path, audio_path], final_path)
        else:
            _run_ffmpeg([video_path], final_path)
        if not final_path.exists() or final_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg 合流后未产出有效文件: {final_path}")
        duration = _probe_duration_seconds(final_path)
        # 成片已就绪，清理中间流
        for path in (video_path, audio_path):
            path.unlink(missing_ok=True)
        return final_path, duration
    except BaseException:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

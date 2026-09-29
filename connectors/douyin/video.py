"""抖音视频能力 connector：直链解析 / 下载合流。

供编排层（video digest）调用，链路：
- resolve_douyin_video_media  用 bsk 打开视频页，从 performance entries 提取 douyinvod 直链
- download_douyin_video       resolve → 立即流式下载 → ffmpeg 合流为 mp4 → ffprobe 读时长

取数姿势（主 Agent 实测验证）：抖音播放器是 MSE（video.src 为 blob:），但播放器发出的
媒体请求会留在 performance.getEntriesByType('resource') 里，douyinvod.com 直链带完整签名，
Python 直接 GET（仅需 UA + Referer）即可下载。直链是 video/audio 分离流，ffmpeg 合流即得
完整 mp4；直链时效约几分钟，拿到必须立刻下载。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

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


@dataclass(slots=True)
class DouyinVideoMedia:
    video_url: str | None  # douyinvod 视频流直链
    audio_url: str | None  # 音频流直链（分离流时非 None；合流场景可能为 None）


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
    """用 bsk 打开视频页，从 resource timing 提取音视频直链。

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
        value: dict | None = None
        # 播放器发媒体请求的时机不固定，且 bsk 会话冷启动后首个抖音页可能加载不出播放器
        # （实测：独立会话能拿到、连续编排偶发拿不到）——最多两次导航机会，每次 3 轮补等。
        for attempt in range(2):
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            for _ in range(3):
                page.wait_for_timeout(PLAYER_WAIT_MS)
                value = page.evaluate(_RESOLVE_JS)
                if isinstance(value, dict) and value.get("media"):
                    break
            if isinstance(value, dict) and value.get("media"):
                break
        # 信任边界校验：页内函数没被真正执行时 bsk 会回函数自身/非 dict（实测，函数按值序列化），
        # 静默当「无直链」会把「提取失败」伪装成「视频不可下载」。
        if not isinstance(value, dict):
            raise RuntimeError(f"抖音视频页内提取返回异常类型: {type(value).__name__}")
        media_urls = [str(u) for u in value.get("media") or [] if u]
        video_url = next((u for u in media_urls if "media-video-" in u), None)
        audio_url = next((u for u in media_urls if "media-audio-" in u), None)
        # 播放器可能只发了音频流（实测个别视频如此）——ASR 只需音轨，仅音频也接受
        if not video_url and not audio_url:
            raise RuntimeError(f"未从抖音视频页提取到可下载直链（同一视频页密集访问会触发抖音风控，冷却几分钟后 --retry-failed 重试通常可过）: {url}")
        video_url = video_url or audio_url
        return DouyinVideoMedia(
            video_url=str(video_url),
            audio_url=str(audio_url) if audio_url else None,
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
) -> tuple[Path, int | None]:
    """下载抖音视频到 dest_dir/<视频id>.mp4，返回 (path, duration_seconds)。

    resolve 后立即流式下载（直链时效约几分钟，不能等）；video+audio 都有则 ffmpeg 合流，
    仅 video 则单流转 mp4 容器。任一响应非 200 / 超 max_bytes / ffmpeg 失败：清理半成品并抛
    RuntimeError。ffprobe 失败不抛，duration 返回 None。
    """
    media = resolve_douyin_video_media(url, session=session, timeout=min(timeout, 60.0))
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

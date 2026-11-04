"""B站视频能力 connector：详情 / CC 字幕 / 直链 / 下载。

供 apps/laterhub/video_digest.py 编排调用，全部走 bsk 页内 fetch（复用日常 Chrome 登录态）：
- get_bilibili_video_meta        拿 aid/cid/title/owner/duration
- get_bilibili_cc_subtitle_text  免下载直取 CC 字幕
- get_bilibili_full_mp4_url      取完整 mp4 直链（喂多模态模型）
- download_bilibili_video        dash 合流 / durl 单文件落盘

为什么在 watchlater 页取数：视频播放页的弹幕/计时器持续自更新，bsk 的整页基线校验
几乎必然判「content changed」，evaluate 会被拒。watchlater 页同属 www.bilibili.com，
Cookie/Origin 一致且页面稳定，足以承载这些接口请求。字幕 JSON 与 CDN 媒体流不需要
Cookie：前者是签名 URL，后者只需浏览器 UA + Referer（实测 206）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
# 播放器信息用非 wbi 的 player/v2：页内直调不需要自行算 w_rid，字幕轨道同样能拿到。
PLAYER_V2_API = "https://api.bilibili.com/x/player/v2"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
BILIBILI_STABLE_PAGE_URL = "https://www.bilibili.com/watchlater/list"
REFERER = "https://www.bilibili.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)
# 页内 fetch 单次超时：字幕/播放地址接口通常秒回，卡住就快速失败。
PAGE_REQUEST_TIMEOUT_MS = 20000

# BV 号固定为 BV + 10 位字母数字；av 号为其后的纯数字
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AV_RE = re.compile(r"/video/av(\d+)", re.IGNORECASE)
_BILI_VIDEO_URL_RE = re.compile(r"bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+)", re.IGNORECASE)

# 页内 fetch 约定：evaluate 的 arg 是 JSON 字符串，函数内 JSON.parse（与订阅动态 JS 一致）。
_BILIBILI_PAGE_FETCH_JS = r"""
async (argsJson) => {
  const args = JSON.parse(argsJson);
  let resp;
  try {
    resp = await fetch(args.url, {
      credentials: "include",
      signal: AbortSignal.timeout(args.requestTimeoutMs),
    });
  } catch (e) {
    return { error: "接口请求失败: " + String(e) };
  }
  try {
    return await resp.json();
  } catch (e) {
    return { error: "接口返回非 JSON（可能登录态失效）" };
  }
}
"""


@dataclass(slots=True)
class BilibiliVideoMeta:
    bvid: str
    aid: int | None
    cid: int | None
    title: str
    owner: str | None
    duration_seconds: int | None


def _download_session() -> requests.Session:
    """CDN 下载会话：浏览器 UA + Referer 即可（qn=16 不需要 Cookie）。"""
    session = requests.Session()
    session.trust_env = False  # B站是国内站点，绕过系统代理（本地代理未开时避免 ProxyError）
    session.headers.update({"User-Agent": USER_AGENT, "Referer": REFERER})
    return session


def _request_json(url: str, *, timeout: float) -> Any:
    session = _download_session()
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _ensure_stable_page(page) -> None:
    """把会话里的 tab 落到稳定的 B站页面；已在 bilibili.com 上则复用。"""
    current = ""
    try:
        current = str(page.url or "")
    except Exception:  # noqa: BLE001 - 读不到 URL 就当不在目标页
        current = ""
    if "bilibili.com" not in current:
        page.goto(BILIBILI_STABLE_PAGE_URL, wait_until="domcontentloaded")
        # 导航后扩展内容脚本需要时间注入新文档；立即 evaluate 会撞
        # 「Receiving end does not exist」，这里先等落定，再由 sync 重试兜底。
        try:
            page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            pass


def _page_api_json(page, api_url: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """bsk 页内 fetch 一个 B站 JSON 接口；code!=0 一律抛 RuntimeError。"""
    _ensure_stable_page(page)
    query = "&".join(f"{key}={quote(str(value))}" for key, value in params.items() if value is not None)
    payload = page.evaluate(
        _BILIBILI_PAGE_FETCH_JS,
        json.dumps({"url": f"{api_url}?{query}", "requestTimeoutMs": PAGE_REQUEST_TIMEOUT_MS}),
        timeout=max(timeout, 30.0),
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"B站接口返回异常（非对象）: {api_url}")
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    if payload.get("code") != 0:
        raise RuntimeError(
            f"B站接口返回异常: code={payload.get('code')} message={payload.get('message')} url={api_url}"
        )
    return payload


def is_bilibili_video_url(url: str) -> bool:
    """是否 B站视频页链接（BV 或 av）。"""
    return bool(url) and bool(_BILI_VIDEO_URL_RE.search(url))


def extract_bilibili_video_id(url: str) -> str:
    """从链接提取视频 ID：BV 号原样返回，av 链接返回 av 号字符串，解析失败返回 ""。"""
    if not url:
        return ""
    match = _BV_RE.search(url)
    if match:
        return match.group(0)
    match = _AV_RE.search(url)
    if match:
        return f"av{match.group(1)}"
    return ""


def _view_payload(page, video_id: str, *, timeout: float) -> dict[str, Any]:
    params = {"bvid": video_id} if video_id.upper().startswith("BV") else {"aid": video_id[2:]}
    return _page_api_json(page, VIEW_API, params, timeout=timeout).get("data") or {}


def get_bilibili_video_meta(url: str, *, page, timeout: float = 30) -> BilibiliVideoMeta:
    """页内调 view 接口拿视频详情。接口异常（code!=0）抛 RuntimeError。"""
    video_id = extract_bilibili_video_id(url)
    if not video_id:
        raise RuntimeError(f"无法从链接解析出 B站视频 ID: {url}")

    payload = _view_payload(page, video_id, timeout=timeout)
    owner = payload.get("owner") or {}
    owner_name = str(owner.get("name") or "").strip()
    return BilibiliVideoMeta(
        bvid=str(payload.get("bvid") or video_id),
        aid=payload.get("aid"),
        cid=payload.get("cid"),
        title=str(payload.get("title") or ""),
        owner=owner_name or None,
        duration_seconds=payload.get("duration"),
    )


# player/v2 在登录态下会随机返回**其他视频**的 AI 字幕轨道（2026-11 实测同一请求
# 约半数串号），必须重试到拿到本视频的轨道；6 次后仍失败按"无可用 CC"处理，由调用方回退 ASR。
CC_TRACK_MAX_ATTEMPTS = 6


def _subtitle_track_belongs_to(track: dict, aid: int | None, cid: int | None) -> bool:
    """校验字幕轨道确实属于本次请求的视频。

    AI 字幕 URL 形如 `/bfs/ai_subtitle/prod/<aid><cid><hash>?auth_key=...`，内嵌 aid+cid
    可直接比对；人工上传字幕不遵循该命名，无法校验时放行（避免误杀）。
    """
    url = str(track.get("subtitle_url") or "").strip()
    if not url:
        return False
    if "ai_subtitle" in url and aid is not None and cid is not None:
        return f"{aid}{cid}" in url
    return True


def get_bilibili_cc_subtitle_text(bvid: str, cid: int | None = None, *, page, timeout: float = 30) -> str | None:
    """取本视频的 CC 字幕文本。

    无可用字幕轨道返回 None；接口错误抛 RuntimeError（不能把接口错误包装成"无字幕"）。
    """
    payload = _view_payload(page, bvid, timeout=timeout)
    aid: int | None = payload.get("aid")
    if cid is None:
        cid = payload.get("cid")
    if cid is None:
        raise RuntimeError(f"无法获取视频 cid: bvid={bvid}")

    params: dict[str, Any] = {"bvid": bvid, "cid": cid}
    if aid is not None:
        params["aid"] = aid
    for _ in range(CC_TRACK_MAX_ATTEMPTS):
        player = _page_api_json(page, PLAYER_V2_API, params, timeout=timeout)
        subtitles = ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        for track in subtitles:
            if not _subtitle_track_belongs_to(track, aid, cid):
                continue
            subtitle_url = str(track.get("subtitle_url") or "").strip()
            if subtitle_url.startswith("//"):
                subtitle_url = f"https:{subtitle_url}"
            body = _request_json(subtitle_url, timeout=timeout)
            cues = (body or {}).get("body") or []
            # 每条 cue 以换行连接，保留字幕的自然断句
            text = "\n".join(str(cue.get("content") or "") for cue in cues).strip()
            if text:
                return text
    return None


def get_bilibili_full_mp4_url(bvid: str, cid: int | None = None, *, page, timeout: float = 30) -> str | None:
    """取完整 mp4 直链（html5 durl 姿势），供多模态模型消费。

    试看截断或 durl 缺失返回 None；接口错误抛 RuntimeError。
    """
    payload = _view_payload(page, bvid, timeout=timeout)
    duration = payload.get("duration")
    if cid is None:
        cid = payload.get("cid")
    if cid is None:
        raise RuntimeError(f"无法获取视频 cid: bvid={bvid}")

    data = _page_api_json(
        page,
        PLAYURL_API,
        {"bvid": bvid, "cid": cid, "qn": 16, "fnval": 0, "platform": "html5", "high_quality": 1},
        timeout=timeout,
    ).get("data") or {}
    durl = data.get("durl") or []
    if not durl:
        return None

    first = durl[0]
    url = str(first.get("url") or "").strip()
    if not url:
        return None
    length_ms = first.get("length")
    # durl.length 是毫秒；明显短于总时长即判定为试看截断
    if duration and isinstance(length_ms, (int, float)) and length_ms < duration * 1000 * 0.95:
        return None
    return url


def _download_stream(
    session: requests.Session,
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    timeout: float,
    already: int = 0,
) -> int:
    """流式下载到 dest，返回累计字节数；超 max_bytes 抛 RuntimeError。"""
    total = already
    with session.get(url, timeout=timeout, stream=True) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"下载超过 max_bytes 限制({max_bytes})，已中止: {url}")
                handle.write(chunk)
    return total


def _merge_with_ffmpeg(video: Path, audio: Path, out: Path) -> None:
    """ffmpeg 无损合流 dash 音视频流（参数数组，禁 shell 拼接）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg 命令，无法合流 dash 音视频流")
    result = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(video), "-i", str(audio), "-c", "copy", str(out)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(f"ffmpeg 合流失败(exit={result.returncode}): {tail[-1] if tail else '无输出'}")


def _verify_duration(path: Path, expected_seconds: float) -> None:
    """ffprobe 校验成片时长 ≥ 预期×0.9，否则视为下载不完整。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("未找到 ffprobe 命令，无法校验下载视频时长")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 校验失败(exit={result.returncode}): {(result.stderr or '').strip()[:200]}")
    try:
        actual = float((json.loads(result.stdout).get("format") or {}).get("duration"))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法解析 ffprobe 输出: {(result.stdout or '').strip()[:200]}") from exc
    if actual < expected_seconds * 0.9:
        raise RuntimeError(f"下载视频时长不完整: 实际 {actual:.1f}s < 预期 {expected_seconds}s×0.9")


def download_bilibili_video(
    bvid: str,
    cid: int | None = None,
    *,
    page,
    dest_dir: Path,
    max_bytes: int = 2**31,
    timeout: float = 1800,
) -> Path:
    """下载视频到 dest_dir/<bvid>.mp4。

    优先 dash（选带宽最小的视频流+音频流，ffmpeg 合流），无 dash 回退 durl 单文件。
    超 max_bytes 抛 RuntimeError 并清理半成品；失败清理所有中间文件。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    session = _download_session()
    payload = _view_payload(page, bvid, timeout=min(timeout, 60))
    duration = payload.get("duration")
    if cid is None:
        cid = payload.get("cid")
    if cid is None:
        raise RuntimeError(f"无法获取视频 cid: bvid={bvid}")

    final_path = dest_dir / f"{bvid}.mp4"
    created: list[Path] = []
    try:
        play = _page_api_json(
            page,
            PLAYURL_API,
            {"bvid": bvid, "cid": cid, "qn": 16, "fnval": 4048, "fnver": 0, "fourk": 1},
            timeout=timeout,
        ).get("data") or {}
        dash = play.get("dash") or {}
        vstreams = dash.get("video") or []
        astreams = dash.get("audio") or []

        if vstreams and astreams:
            v0 = min(vstreams, key=lambda x: x.get("bandwidth") or 0)
            a0 = min(astreams, key=lambda x: x.get("bandwidth") or 0)
            vpath = dest_dir / f"{bvid}.video.m4s"
            apath = dest_dir / f"{bvid}.audio.m4s"
            created += [vpath, apath]
            total = _download_stream(session, v0["baseUrl"], vpath, max_bytes=max_bytes, timeout=timeout)
            _download_stream(session, a0["baseUrl"], apath, max_bytes=max_bytes, timeout=timeout, already=total)
            _merge_with_ffmpeg(vpath, apath, final_path)
            if not final_path.exists() or final_path.stat().st_size == 0:
                raise RuntimeError(f"ffmpeg 合流后未产出有效文件: {final_path}")
            if duration:
                _verify_duration(final_path, duration)
            # 成片已就绪，清理中间流
            for path in (vpath, apath):
                path.unlink(missing_ok=True)
            created.clear()
            return final_path

        durl = play.get("durl") or []
        if not durl:
            # dash 与 durl 皆缺：回退 html5 durl 端点再试一次
            fallback = _page_api_json(
                page,
                PLAYURL_API,
                {"bvid": bvid, "cid": cid, "qn": 16, "fnval": 0, "platform": "html5", "high_quality": 1},
                timeout=timeout,
            ).get("data") or {}
            durl = fallback.get("durl") or []
        if not durl or not str(durl[0].get("url") or "").strip():
            raise RuntimeError(f"未获取到可下载的视频流: bvid={bvid}")

        created.append(final_path)
        _download_stream(session, str(durl[0]["url"]), final_path, max_bytes=max_bytes, timeout=timeout)
        if not final_path.exists() or final_path.stat().st_size == 0:
            raise RuntimeError(f"下载后未产出有效文件: {final_path}")
        created.clear()
        return final_path
    except BaseException:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

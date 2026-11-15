"""只读探测：收藏列表接口在 bsk 页内直调是否可用、返回哪些字段、play_addr 能否直接下载。

不改库、不写业务文件；只发 1~2 次列表请求 + 1 次 1KB 范围请求验证 CDN 是否放行。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import requests

from connectors._shared.bsk_session import BskRunnerSession
from connectors.douyin.favorites import DOUYIN_STABILIZE_MEDIA_JS

FAV_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"

API_JS = r"""
async (cursor) => {
  const params = new URLSearchParams({
    device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
    pc_client_type: '1', version_code: '170400', version_name: '17.4.0',
    cookie_enabled: 'true', platform: 'PC', browser_language: 'zh-CN',
    browser_platform: 'Win32', browser_name: 'Chrome', browser_online: 'true',
    engine_name: 'Blink', os_name: 'Windows', os_version: '10',
    count: '20', cursor: String(cursor || 0),
  });
  try {
    const resp = await fetch('/aweme/v1/web/aweme/listcollection/?' + params.toString(), {
      credentials: 'include', signal: AbortSignal.timeout(20000),
    });
    const text = await resp.text();
    let parsed = null;
    try { parsed = JSON.parse(text); } catch (e) {}
    if (!parsed) return { status: resp.status, bytes: text.length, head: text.slice(0, 300) };
    const list = parsed.aweme_list || [];
    const sample = list.slice(0, 3).map((a) => ({
      aweme_id: a.aweme_id,
      desc: (a.desc || '').slice(0, 60),
      author: (a.author || {}).nickname || '',
      duration: (a.video || {}).duration || a.duration || 0,
      collect_time: a.collect_time || null,
      aweme_type: a.aweme_type,
      play_hosts: ((a.video || {}).play_addr || {}).url_list || [],
      has_download_addr: Boolean(((a.video || {}).download_addr || {}).url_list),
      subtitle_infos: ((a.video || {}).subtitle_infos || []).length,
      image_count: (a.images || []).length,
      status_ok: Boolean(((a.video || {}).play_addr || {}).url_list),
    }));
    return {
      status: resp.status,
      status_code: parsed.status_code,
      has_more: parsed.has_more,
      cursor: parsed.cursor,
      total: parsed.total ?? null,
      count: list.length,
      all_keys: list[0] ? Object.keys(list[0]).slice(0, 40) : [],
      sample,
    };
  } catch (e) {
    return { error: String(e) };
  }
}
"""


def _cdn_probe(url: str) -> dict:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
            "Range": "bytes=0-1023",
        }
    )
    try:
        resp = session.get(url, timeout=20, stream=True)
        data = next(resp.iter_content(1024), b"")
        return {
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "content_range": resp.headers.get("content-range"),
            "first_bytes": len(data),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    session = BskRunnerSession(start_url="about:blank")
    session.start()
    page = session.acquire_page()
    report: dict = {}
    try:
        page.goto(FAV_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        page.evaluate(DOUYIN_STABILIZE_MEDIA_JS)
        report["page1"] = page.evaluate(API_JS, 0)
        page.wait_for_timeout(1500)
        report["page2_cursor20"] = page.evaluate(API_JS, 20)
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        session.shutdown()

    urls: list[str] = []
    for key in ("page1", "page2_cursor20"):
        for item in (report.get(key) or {}).get("sample", []) or []:
            urls.extend(item.get("play_hosts") or [])
    if urls:
        report["cdn"] = _cdn_probe(urls[0])

    out = Path(__file__).with_name("dy_list_probe2.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

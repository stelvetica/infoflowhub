"""只读探测：收藏列表接口是否可用、是否返回 collect_time；页面里有无「收藏于」类时间。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from connectors._shared.bsk_session import BskRunnerSession
from connectors.douyin.favorites import DOUYIN_STABILIZE_MEDIA_JS

FAV_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"

API_JS = r"""
async () => {
  const params = new URLSearchParams({
    device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
    pc_client_type: '1', version_code: '170400', version_name: '17.4.0',
    cookie_enabled: 'true', platform: 'PC', browser_language: 'zh-CN',
    browser_platform: 'Win32', browser_name: 'Chrome', browser_online: 'true',
    engine_name: 'Blink', os_name: 'Windows', os_version: '10',
    count: '20', cursor: '0',
  });
  try {
    const resp = await fetch('/aweme/v1/web/aweme/listcollection/?' + params.toString(), {
      credentials: 'include', signal: AbortSignal.timeout(15000),
    });
    const text = await resp.text();
    let parsed = null;
    try { parsed = JSON.parse(text); } catch (e) {}
    const list = parsed && (parsed.aweme_list || parsed.data || []);
    const first = list && list[0] ? list[0] : null;
    return {
      status: resp.status,
      bytes: text.length,
      hasMore: parsed ? parsed.has_more : null,
      cursor: parsed ? parsed.cursor : null,
      count: Array.isArray(list) ? list.length : null,
      sampleKeys: first ? Object.keys(first).slice(0, 12) : [],
      sampleCollectTime: first ? (first.collect_time || first.collectTime || null) : null,
      head: text.slice(0, 200),
    };
  } catch (e) {
    return { error: String(e) };
  }
}
"""

DOM_JS = r"""
() => {
  const texts = [];
  document.querySelectorAll('*').forEach((n) => {
    if (n.children.length) return;
    const t = (n.textContent || '').trim();
    if (t && /收藏于|收藏时间|\d{4}-\d{2}-\d{2}/.test(t) && t.length < 40) texts.push(t);
  });
  const sorts = Array.from(document.querySelectorAll('button,span,div')).map((n) => (n.textContent || '').trim())
    .filter((t) => ['最新', '最早', '最多播放', '最近收藏'].includes(t)).slice(0, 8);
  return { timeTexts: Array.from(new Set(texts)).slice(0, 10), sortControls: Array.from(new Set(sorts)) };
}
"""


def main() -> int:
    session = BskRunnerSession(start_url="about:blank")
    session.start()
    page = session.acquire_page()
    report = {}
    try:
        page.goto(FAV_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        page.evaluate(DOUYIN_STABILIZE_MEDIA_JS)
        report["api"] = page.evaluate(API_JS)
        report["dom"] = page.evaluate(DOM_JS)
    except Exception as exc:
        report["error"] = str(exc)[:200]
    finally:
        session.shutdown()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

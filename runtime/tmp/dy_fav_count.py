import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from connectors._shared.bsk_session import BskRunnerSession
from connectors.douyin.favorites import DOUYIN_STABILIZE_MEDIA_JS

FAV_URL = "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"
JS = r"""
() => {
  const leaves = Array.from(document.querySelectorAll('*')).filter((n) => !n.children.length);
  const hits = [];
  for (const n of leaves) {
    const t = (n.textContent || '').trim();
    if (!t || t.length > 30) continue;
    if (!/收藏|喜欢|作品|稍后再看/.test(t)) continue;
    const p = n.parentElement;
    hits.push({ text: t, parent: p ? (p.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40) : '' });
  }
  return hits.slice(0, 25);
}
"""

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = BskRunnerSession(start_url="about:blank"); s.start()
    page = s.acquire_page()
    try:
        page.goto(FAV_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)
        page.evaluate(DOUYIN_STABILIZE_MEDIA_JS)
        print(json.dumps(page.evaluate(JS), ensure_ascii=False, indent=2))
    finally:
        s.shutdown()

main()

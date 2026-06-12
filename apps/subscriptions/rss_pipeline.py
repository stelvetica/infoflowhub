from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.subscriptions.importers import parse_opml
from apps.subscriptions.rss_config import load_settings, load_sources, save_sources
from apps.subscriptions.rss_db import save_entries
from connectors.alphapai import fetch_alphapai_source
from connectors.rss.fetch import fetch_many, trim_fetch_result


BASE_DIR = Path(__file__).resolve().parents[2]


def _safe_print(text: str) -> None:
    print(text)


def import_opml(opml_path: str, output_path: str = "") -> int:
    rows = parse_opml(opml_path)
    target = Path(output_path) if output_path else BASE_DIR / "output" / "opml_import_preview.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _safe_print(f"已解析 {len(rows)} 个订阅源: {target}")
    return 0


def list_sources() -> int:
    sources = load_sources()
    if not sources:
        _safe_print("当前没有配置 RSS 源")
        return 0
    for item in sources:
        mark = "ON" if item.get("enabled", False) else "OFF"
        provider = item.get("provider", item.get("kind", "native"))
        fetch_via = item.get("fetch_via", "direct")
        _safe_print(f"[{mark}] {item['id']} | {item['name']} | {provider} | {fetch_via} | {item['feed_url']}")
    return 0


def fetch_enabled(source_id: str = "") -> int:
    sources = [item for item in load_sources() if item.get("enabled", False)]
    if source_id:
        sources = [item for item in sources if item.get("id") == source_id]
    settings = load_settings()
    if not sources:
        _safe_print("当前没有命中的启用订阅源")
        return 0

    if len(sources) == 1 and sources[0].get("id") == "alphapai":
        result = fetch_alphapai_source(sources[0])
        result = trim_fetch_result(result)
        if not result.ok:
            _safe_print(f"FAIL {result.source_name}: {result.error or result.status}")
            return 1
        inserted = save_entries(result.entries)
        _safe_print(f"OK {result.source_name}: 抓取 {len(result.entries)} 条, 新增 {inserted} 条")
        _safe_print(f"完成: 新增 {inserted} 条")
        return 0

    results = fetch_many(sources, settings=settings)
    total_inserted = 0
    failed = False
    for result in results:
        if not result.ok:
            _safe_print(f"FAIL {result.source_name}: {result.error or result.status}")
            failed = True
            continue
        inserted = save_entries(result.entries)
        total_inserted += inserted
        _safe_print(f"OK {result.source_name}: 抓取 {len(result.entries)} 条, 新增 {inserted} 条")
    _safe_print(f"完成: 新增 {total_inserted} 条")
    return 1 if failed else 0


def init_sources() -> int:
    example_path = BASE_DIR / "config" / "subscription_sources.example.json"
    sources = json.loads(example_path.read_text(encoding="utf-8")).get("sources", [])
    save_sources(sources)
    _safe_print("已初始化 subscription_sources.json")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="subscriptions RSS pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-sources")
    subparsers.add_parser("list")
    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--source", default="")
    import_parser = subparsers.add_parser("import-opml")
    import_parser.add_argument("--path", required=True)
    import_parser.add_argument("--output", default="")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "init-sources":
        return init_sources()
    if args.command == "list":
        return list_sources()
    if args.command == "fetch":
        return fetch_enabled(getattr(args, "source", ""))
    if args.command == "import-opml":
        return import_opml(args.path, args.output)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

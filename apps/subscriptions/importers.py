from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse


def _slugify(value: str) -> str:
    chars = []
    for char in value.lower():
        chars.append(char if char.isalnum() else "-")
    slug = "".join(chars).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "rss-source"


def parse_opml(path: str) -> List[Dict]:
    root = ET.parse(Path(path)).getroot()
    body = root.find("body")
    if body is None:
        return []

    rows: List[Dict] = []
    for group in body.findall("outline"):
        group_name = group.attrib.get("text", "").strip() or "未分组"
        for item in group.findall("outline"):
            feed_url = item.attrib.get("xmlUrl", "").strip()
            if not feed_url:
                continue
            name = item.attrib.get("title", "").strip() or item.attrib.get("text", "").strip()
            site_url = item.attrib.get("htmlUrl", "").strip()
            host = urlparse(feed_url).netloc
            rows.append(
                {
                    "id": _slugify(name),
                    "name": name,
                    "group": group_name,
                    "feed_url": feed_url,
                    "site_url": site_url,
                    "host": host,
                    "kind": "rsshub" if "rsshub" in host.lower() else "native",
                    "enabled": False,
                }
            )
    return rows

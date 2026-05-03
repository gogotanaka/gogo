#!/usr/bin/env python3
"""Fetch MAIN Notion page blocks and output as plain text."""
import json
import sys
import urllib.request
from pathlib import Path

PAGE_ID = "227efe00a9138021b471c26bfccccf21"
API_KEY = Path("~/.config/notion/api_key").expanduser().read_text().strip()


def fetch_blocks(block_id, depth=0):
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Notion-Version": "2025-09-03",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())

    lines = []
    for b in data.get("results", []):
        t = b["type"]
        node = b.get(t, {})
        rich = node.get("rich_text", [])
        text = "".join(x.get("plain_text", "") for x in rich).strip()

        prefix = "  " * depth
        if t == "heading_1":
            lines.append(f"\n{prefix}# {text}")
        elif t == "heading_2":
            lines.append(f"\n{prefix}## {text}")
        elif t == "heading_3":
            lines.append(f"\n{prefix}### {text}")
        elif t == "to_do":
            checked = "x" if node.get("checked") else " "
            if text:
                lines.append(f"{prefix}- [{checked}] {text}")
        elif t == "bulleted_list_item" or t == "numbered_list_item":
            if text:
                lines.append(f"{prefix}- {text}")
        elif t == "paragraph":
            if text:
                lines.append(f"{prefix}{text}")
        elif t == "toggle":
            if text:
                lines.append(f"{prefix}▸ {text}")
        elif t == "quote":
            if text:
                lines.append(f"{prefix}> {text}")

        if b.get("has_children"):
            child_lines = fetch_blocks(b["id"], depth + 1)
            lines.extend(child_lines)

    return lines


def main():
    lines = fetch_blocks(PAGE_ID)
    print("\n".join(lines))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Write the finalized daily briefing to Notion (idempotent).

Input: a JSON file describing the page title, sections, tables and news paragraphs.
This script does NOT call external sites; it only writes to Notion.

Usage:
  python3 write_notion_from_briefing.py --in briefing.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.request
from typing import Any, Optional

NOTION_VERSION = "2022-06-28"
NOTION_TOKEN_PATH = os.path.expanduser("~/.openclaw/credentials/notion.token")
NOTION_DATABASE_ID = "315c392a8f7a80cbb1b6d16994e18f58"  # Document Hub
CATEGORY_NAME = "Riepilogo Sportivo Giornaliero"


def notion_json(method: str, path: str, token: str, body: Any | None = None) -> Any:
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def rt(text: str, link: Optional[str] = None) -> list[dict[str, Any]]:
    t: dict[str, Any] = {"type": "text", "text": {"content": text}}
    if link:
        t["text"]["link"] = {"url": link}
    return [t]


def paragraph_rich(rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}


def heading2(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rt(text)}}


def heading3(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rt(text)}}


def table_block(header: list[str], rows: list[list[str]]) -> dict[str, Any]:
    children = [
        {"object": "block", "type": "table_row", "table_row": {"cells": [rt(h) for h in header]}}
    ]
    for r in rows:
        children.append({"object": "block", "type": "table_row", "table_row": {"cells": [rt(c) for c in r]}})
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": len(header),
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        },
    }


def notion_find_page_by_title(token: str, title: str) -> Optional[dict[str, Any]]:
    body = {"filter": {"property": "Doc name", "title": {"equals": title}}, "page_size": 5}
    res = notion_json("POST", f"databases/{NOTION_DATABASE_ID}/query", token, body)
    results = res.get("results", [])
    return results[0] if results else None


def notion_create_page(token: str, title: str) -> dict[str, Any]:
    body = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Doc name": {"title": [{"type": "text", "text": {"content": title}}]},
            "Category": {"multi_select": [{"name": CATEGORY_NAME}]},
        },
    }
    return notion_json("POST", "pages", token, body)


def notion_replace_children(token: str, page_id: str, new_children: list[dict[str, Any]]) -> None:
    kids = notion_json("GET", f"blocks/{page_id}/children?page_size=100", token)
    for b in kids.get("results", []):
        bid = b.get("id")
        if bid:
            try:
                notion_json("DELETE", f"blocks/{bid}", token)
            except Exception:
                pass
    notion_json("PATCH", f"blocks/{page_id}/children", token, {"children": new_children})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    args = ap.parse_args()

    token = open(NOTION_TOKEN_PATH, "r", encoding="utf-8").read().strip()
    briefing = json.loads(open(args.inp, "r", encoding="utf-8").read())

    title = briefing["pageTitle"]
    children: list[dict[str, Any]] = []

    intro = briefing.get("intro") or ""
    if intro:
        children.append(paragraph_rich(rt(intro)))

    for sec in briefing.get("sections", []):
        children.append(heading2(sec["title"]))
        if sec.get("table"):
            t = sec["table"]
            children.append(table_block(t["header"], t["rows"]))
        news = sec.get("news", [])
        if news:
            children.append(heading3("Top news"))
            for item in news:
                # requirement: one paragraph recap + full link
                # format: [Title (linked)] — recap
                rich: list[dict[str, Any]] = []
                rich += rt(item["title"], link=item["link"])
                recap = (item.get("recap") or "").strip()
                if recap:
                    rich.append({"type": "text", "text": {"content": f" — {recap}"}})
                children.append(paragraph_rich(rich))

    page = notion_find_page_by_title(token, title)
    if page is None:
        page = notion_create_page(token, title)
    notion_replace_children(token, page["id"], children)

    print(json.dumps({"ok": True, "title": title, "url": page.get("url")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

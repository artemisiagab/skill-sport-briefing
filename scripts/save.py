#!/usr/bin/env python3
"""Read a briefing JSON file and write/update the corresponding Notion page.

This script is step 2 of the daily briefing pipeline:
  1. daily_briefing.py  → fetches live data, writes briefing.json
  2. save.py            → reads briefing.json, writes/updates a Notion page

The operation is idempotent:
  - If a page with the same title already exists, its content is replaced.
  - If no page exists, a new one is created in the Document Hub database.

Expected input schema (briefing.json):
  {
    "pageTitle": "Riepilogo Sportivo Giornaliero del YYYY-MM-DD",
    "intro": "Riepilogo automatico (eventi + notizie selezionate).",
    "sections": [
      {
        "title": "Section Name",
        "table": { "header": ["Col1", ...], "rows": [["val1", ...], ...] },
        "news": [
          { "title": "Article title", "link": "https://...", "recap": "..." }
        ]
      }, ...
    ]
  }

Usage:
  python3 save.py [--in /path/to/briefing.json]

If --in is omitted the default path is:
  ~/.openclaw/workspace/reports/daily-briefing/briefing.json

Notion token is read from:
  ~/.openclaw/credentials/notion.token
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Notion API version used in the Notion-Version header.
NOTION_VERSION = "2022-06-28"

# Path to the file containing the Notion integration token (one line).
NOTION_TOKEN_PATH = os.path.expanduser("~/.openclaw/credentials/notion.token")

# The Notion database where briefing pages are stored ("Document Hub").
NOTION_DATABASE_ID = "315c392a8f7a80cbb1b6d16994e18f58"

# Multi-select tag applied to every briefing page for easy filtering.
CATEGORY_NAME = "Riepilogo Sportivo Giornaliero"

# Default input path — matches the default output of gather_payload.py.
DEFAULT_IN = os.path.expanduser(
    "~/.openclaw/workspace/reports/sport-briefing/briefing.json"
)

# Default output path for the daily markdown file.
DEFAULT_MD_OUT = os.path.expanduser(
    "~/.openclaw/workspace/reports/sport-briefing/daily.md"
)


# ---------------------------------------------------------------------------
# Notion API helpers (stdlib only — no third-party HTTP libraries)
# ---------------------------------------------------------------------------


def notion_json(method: str, path: str, token: str, body: Any | None = None) -> Any:
    """Send a request to the Notion API and return the parsed JSON response.

    Args:
        method: HTTP method (GET, POST, PATCH, DELETE).
        path: API path relative to https://api.notion.com/v1/.
        token: Notion integration bearer token.
        body: Optional dict/list to send as JSON body.
    """
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


# ---------------------------------------------------------------------------
# Notion block builders — turn raw data into Notion block dicts
# ---------------------------------------------------------------------------


def rt(text: str, link: Optional[str] = None) -> list[dict[str, Any]]:
    """Build a Notion rich-text array containing a single text span.

    If *link* is provided, the text becomes a clickable hyperlink.
    """
    t: dict[str, Any] = {"type": "text", "text": {"content": text}}
    if link:
        t["text"]["link"] = {"url": link}
    return [t]


def paragraph_rich(rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Notion paragraph block from an already-constructed rich-text list."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text},
    }


def heading2(text: str) -> dict[str, Any]:
    """Build a Notion heading_2 block (## level)."""
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": rt(text)},
    }


def heading3(text: str) -> dict[str, Any]:
    """Build a Notion heading_3 block (### level)."""
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": rt(text)},
    }


def table_block(header: list[str], rows: list[list[str]]) -> dict[str, Any]:
    """Build a Notion table block with a header row and data rows.

    Args:
        header: Column names (e.g. ["Match", "When", "Competition", "Round"]).
        rows: List of row lists — each inner list has one string per column.
    """
    # First child row is the column header.
    children = [
        {
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": [rt(h) for h in header]},
        }
    ]
    # Data rows follow.
    for r in rows:
        children.append(
            {
                "object": "block",
                "type": "table_row",
                "table_row": {"cells": [rt(c) for c in r]},
            }
        )
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


# ---------------------------------------------------------------------------
# Notion page CRUD — find, create, and replace page content
# ---------------------------------------------------------------------------


def notion_find_page_by_title(token: str, title: str) -> Optional[dict[str, Any]]:
    """Search the Document Hub database for a page whose title matches exactly.

    Returns the first matching page dict, or None if no match is found.
    """
    body = {
        "filter": {"property": "Doc name", "title": {"equals": title}},
        "page_size": 5,
    }
    res = notion_json("POST", f"databases/{NOTION_DATABASE_ID}/query", token, body)
    results = res.get("results", [])
    return results[0] if results else None


def notion_create_page(token: str, title: str) -> dict[str, Any]:
    """Create a new page in the Document Hub database with the given title.

    The page is tagged with the "Riepilogo Sportivo Giornaliero" category.
    """
    body = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Doc name": {"title": [{"type": "text", "text": {"content": title}}]},
            "Category": {"multi_select": [{"name": CATEGORY_NAME}]},
        },
    }
    return notion_json("POST", "pages", token, body)


def notion_replace_children(token: str, page_id: str, new_children: list[dict[str, Any]]) -> None:
    """Delete all existing child blocks of a page and append *new_children*.

    This makes the write idempotent: running the script twice with the same
    input produces the same page content — no duplicates, no stale data.
    """
    # Step 1 — fetch current children (up to 100; enough for our use case).
    kids = notion_json("GET", f"blocks/{page_id}/children?page_size=100", token)

    # Step 2 — delete each child block.
    for b in kids.get("results", []):
        bid = b.get("id")
        if bid:
            try:
                notion_json("DELETE", f"blocks/{bid}", token)
            except Exception:
                # Silently ignore already-deleted or inaccessible blocks.
                pass

    # Step 3 — append the fresh content.
    notion_json("PATCH", f"blocks/{page_id}/children", token, {"children": new_children})


# ---------------------------------------------------------------------------
# Briefing → Notion blocks conversion
# ---------------------------------------------------------------------------


def briefing_to_blocks(briefing: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a full briefing dict into a flat list of Notion blocks.

    The resulting block list is ready to be appended as page children:
      1. Intro paragraph
      2. For each section:
         a. Heading (## Section Title)
         b. Table (events / sessions)
         c. If news items exist: ### Top news + one paragraph per item
    """
    children: list[dict[str, Any]] = []

    # Intro line — appears at the very top of the page.
    intro = briefing.get("intro") or ""
    if intro:
        children.append(paragraph_rich(rt(intro)))

    # Iterate over sections (Fiorentina, AC Milan, Sinner, …).
    for sec in briefing.get("sections", []):
        # Section heading (rendered as ## in Notion).
        children.append(heading2(sec["title"]))

        # Event table — always present, may have zero data rows.
        if sec.get("table"):
            t = sec["table"]
            children.append(table_block(t["header"], t["rows"]))

        # News items — 0–4 per section.  Each item is a single paragraph:
        #   [Article title (linked)] — Italian recap text
        news = sec.get("news", [])
        if news:
            children.append(heading3("Top news"))
            for item in news:
                rich: list[dict[str, Any]] = []
                # The title is hyperlinked to the article URL.
                rich += rt(item["title"], link=item["link"])
                # The recap follows after an em-dash.
                recap = (item.get("recap") or "").strip()
                if recap:
                    rich.append({"type": "text", "text": {"content": f" — {recap}"}})
                children.append(paragraph_rich(rich))

    return children


def briefing_to_markdown(briefing: dict[str, Any]) -> str:
    """Convert a briefing dict into a Markdown string (daily.md)."""
    lines: list[str] = []
    lines.append(f"# {briefing['pageTitle']}\n")
    intro = briefing.get("intro", "")
    if intro:
        lines.append(f"{intro}\n")
    for sec in briefing.get("sections", []):
        lines.append(f"## {sec['title']}\n")
        table = sec.get("table")
        if table:
            header = table["header"]
            rows = table["rows"]
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join(["---"] * len(header)) + " |")
            for row in rows:
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")
        news = sec.get("news", [])
        if news:
            lines.append("### Top news\n")
            for item in news:
                title = item["title"]
                link = item["link"]
                recap = (item.get("recap") or "").strip()
                line = f"[{title}]({link})"
                if recap:
                    line += f" — {recap}"
                lines.append(line + "\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main — read briefing.json and push to Notion
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read a briefing JSON file and write it to Notion (idempotent).",
    )
    ap.add_argument(
        "--in", dest="inp",
        default=DEFAULT_IN,
        help="Path to the briefing JSON file (default: %(default)s)",
    )
    ap.add_argument(
        "--md-out",
        default=DEFAULT_MD_OUT,
        help="Path to write the daily markdown file (default: %(default)s)",
    )
    args = ap.parse_args()

    # ---- Load Notion token ----
    token = open(NOTION_TOKEN_PATH, "r", encoding="utf-8").read().strip()
    if not token:
        print("Notion token file is empty.", file=sys.stderr)
        return 2

    # ---- Load briefing JSON ----
    briefing = json.loads(open(args.inp, "r", encoding="utf-8").read())
    title = briefing["pageTitle"]

    # ---- Convert the briefing structure into Notion blocks ----
    children = briefing_to_blocks(briefing)

    # ---- Find or create the Notion page ----
    page = notion_find_page_by_title(token, title)
    if page is None:
        page = notion_create_page(token, title)

    # ---- Replace all page content (idempotent) ----
    notion_replace_children(token, page["id"], children)

    print(title)
    print("Updated Notion page:", page.get("url"))

    # ---- Generate daily.md ----
    md = briefing_to_markdown(briefing)
    os.makedirs(os.path.dirname(os.path.abspath(args.md_out)), exist_ok=True)
    with open(args.md_out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Markdown written to {args.md_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

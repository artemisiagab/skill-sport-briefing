#!/usr/bin/env python3
"""Daily sports briefing: Sofascore + RSS -> Notion (idempotent).

- Pull upcoming events via Sofascore APIs.
- Pull top news items (0-4 each topic) via RSS feeds.
- Write an idempotent daily page in Notion (Document Hub), with separate sections/tables.

Notes
- Notion token is read from ~/.openclaw/credentials/notion.token
- Date formatting is human-friendly (e.g. "Next Tuesday at 20:45 (Tue 02.Mar)",
  "In 2 weeks at 20:45 (Tue 22.Mar)").
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

ROME = ZoneInfo("Europe/Rome")
UTC = dt.timezone.utc

NOTION_VERSION = "2022-06-28"
NOTION_TOKEN_PATH = os.path.expanduser("~/.openclaw/credentials/notion.token")
NOTION_DATABASE_ID = "315c392a8f7a80cbb1b6d16994e18f58"  # Document Hub
CATEGORY_NAME = "Riepilogo Sportivo Giornaliero"

# Sofascore
SOFASCORE = "https://api.sofascore.com/api/v1"
FIORENTINA_ID = 2693
MILAN_ID = 2692
SINNER_TEAM_ID = 206570  # Sofascore tennis player/team entity
IT_VOLLEY_M_ID = 6824
IT_VOLLEY_F_ID = 6709
F1_UNIQUE_STAGE_ID = 40
MOTOGP_UNIQUE_STAGE_ID = 17

# News feeds
RSS_SERIE_A = "https://www.gazzetta.it/dynamic-feed/rss/section/Calcio/Serie-A.xml"
RSS_TENNIS = "https://www.gazzetta.it/dynamic-feed/rss/section/Tennis.xml"
RSS_MOTOGP = "https://www.gazzetta.it/dynamic-feed/rss/section/Moto/moto-GP.xml"
RSS_F1 = "https://www.gazzetta.it/dynamic-feed/rss/section/Formula-1.xml"
F1_NEWS_FP = "https://www.formulapassion.it/f1/f1-news"

# Volleyball: keep only major competitions (no friendlies)
VOLLEY_MAJOR_PATTERNS = [
    re.compile(r"VNL|Nations League", re.I),
    re.compile(r"World Championship|World Cup", re.I),
    re.compile(r"European Championship|Euro", re.I),
]


@dataclass
class Row:
    cells: list[str]


@dataclass
class NewsItem:
    title: str
    link: str
    summary: str = ""


def http_text(url: str, headers: Optional[dict[str, str]] = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def http_json(url: str, headers: Optional[dict[str, str]] = None) -> Any:
    return json.loads(http_text(url, headers=headers))


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


# ----------------------- date formatting -----------------------

EN_WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EN_WEEKDAY_FULL = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
EN_MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def today_rome() -> dt.date:
    return dt.datetime.now(tz=ROME).date()


def human_when(ts: int, now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(tz=ROME)
    d = dt.datetime.fromtimestamp(ts, tz=ROME)
    days = (d.date() - now.date()).days
    hhmm = d.strftime("%H:%M")
    suffix = f"({EN_WEEKDAY[d.weekday()]} {d.day:02d}.{EN_MONTH[d.month-1]})"

    if days == 0:
        return f"Today at {hhmm} {suffix}"
    if days == 1:
        return f"Tomorrow at {hhmm} {suffix}"
    if 2 <= days <= 7:
        return f"Next {EN_WEEKDAY_FULL[d.weekday()]} at {hhmm} {suffix}"

    if days >= 14:
        weeks = days // 7
        return f"In {weeks} weeks at {hhmm} {suffix}"

    return f"In {days} days at {hhmm} {suffix}"


# ----------------------- Notion blocks helpers -----------------------


def rt(text: str, link: Optional[str] = None) -> list[dict[str, Any]]:
    t: dict[str, Any] = {"type": "text", "text": {"content": text}}
    if link:
        t["text"]["link"] = {"url": link}
    return [t]


def heading(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rt(text)}}


def heading3(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rt(text)}}


def paragraph(parts: list[dict[str, Any]] | str) -> dict[str, Any]:
    rich_text = rt(parts) if isinstance(parts, str) else parts
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}


def table_block(rows: list[Row], header: list[str]) -> dict[str, Any]:
    children = [
        {"object": "block", "type": "table_row", "table_row": {"cells": [rt(h) for h in header]}}
    ]
    for r in rows:
        children.append(
            {"object": "block", "type": "table_row", "table_row": {"cells": [rt(c) for c in r.cells]}}
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


# ----------------------- Sofascore fetchers -----------------------


def next_team_events(team_id: int, limit: int = 2) -> list[dict[str, Any]]:
    data = http_json(f"{SOFASCORE}/team/{team_id}/events/next/0")
    ev = sorted(data.get("events", []), key=lambda e: e.get("startTimestamp", 10**18))
    return ev[:limit]


def sinner_events_fallback(limit: int = 2) -> list[dict[str, Any]]:
    # Tennis doesn't reliably expose /events/next for this entity; fallback: use last and pick upcoming.
    data = http_json(f"{SOFASCORE}/team/{SINNER_TEAM_ID}/events/last/0")
    ev = data.get("events", [])
    now_ts = int(dt.datetime.now(tz=UTC).timestamp())

    def status_type(e: dict[str, Any]) -> str:
        return (e.get("status") or {}).get("type") or ""

    upcoming = [e for e in ev if e.get("startTimestamp", 0) >= now_ts and status_type(e) in {"notstarted", "inprogress"}]
    return sorted(upcoming, key=lambda e: e.get("startTimestamp", 10**18))[:limit]


def volley_next_major_events(team_id: int, limit: int = 2) -> list[dict[str, Any]]:
    data = http_json(f"{SOFASCORE}/team/{team_id}/events/next/0")
    ev = data.get("events", [])

    def is_major(e: dict[str, Any]) -> bool:
        t = e.get("tournament") or {}
        uniq = t.get("uniqueTournament") or {}
        ut_name = (uniq.get("name") or t.get("name") or "")
        cat = (t.get("category") or {}).get("name") or (uniq.get("category") or {}).get("name")
        if (cat or "").lower() != "international":
            return False
        return any(p.search(ut_name) for p in VOLLEY_MAJOR_PATTERNS)

    ev = [e for e in ev if is_major(e)]
    return sorted(ev, key=lambda e: e.get("startTimestamp", 10**18))[:limit]


def find_next_stage_id(unique_stage_id: int) -> Optional[int]:
    # Quick search-based discovery of next stage.
    q = "formula 1" if unique_stage_id == F1_UNIQUE_STAGE_ID else "motogp"
    data = http_json(f"{SOFASCORE}/search/all?q={urllib.request.quote(q)}")
    now_ts = int(dt.datetime.now(tz=UTC).timestamp())

    candidates: list[dict[str, Any]] = []
    for r in data.get("results", []):
        if r.get("type") != "stage":
            continue
        ent = r.get("entity") or {}
        cat = (ent.get("category") or {}).get("name") or ""
        cat_l = cat.lower()
        if unique_stage_id == F1_UNIQUE_STAGE_ID and "formula" not in cat_l:
            continue
        if unique_stage_id == MOTOGP_UNIQUE_STAGE_ID and "motogp" not in cat_l:
            continue
        st = ent.get("startTimestamp")
        if not isinstance(st, int) or st < now_ts:
            continue
        candidates.append(ent)

    candidates = sorted(candidates, key=lambda e: e.get("startTimestamp", 10**18))
    return int(candidates[0]["id"]) if candidates else None


def stage_summary(stage_id: int) -> dict[str, str]:
    data = http_json(f"{SOFASCORE}/stage/{stage_id}")
    st = data.get("stage") or {}
    name = st.get("name") or st.get("description") or f"Stage {stage_id}"
    country = (st.get("country") or {}).get("name")
    info = st.get("info") or {}
    circuit = info.get("circuit")
    city = info.get("circuitCity")
    return {
        "name": str(name),
        "country": str(country or ""),
        "circuit": str(circuit or ""),
        "city": str(city or ""),
    }


def stage_substages(stage_id: int) -> list[dict[str, Any]]:
    # Motorsports sessions live under www.sofascore.com/api/v1.
    data = http_json(f"https://www.sofascore.com/api/v1/stage/{stage_id}/substages")
    stages = data.get("stages", [])
    return sorted(stages, key=lambda s: s.get("startDateTimestamp", 10**18))


# ----------------------- rows builders -----------------------


def build_match_rows(events: list[dict[str, Any]]) -> list[Row]:
    rows: list[Row] = []
    now = dt.datetime.now(tz=ROME)
    for e in events:
        home = (e.get("homeTeam") or {}).get("name") or "?"
        away = (e.get("awayTeam") or {}).get("name") or "?"
        ts = e.get("startTimestamp")
        when = human_when(int(ts), now=now) if isinstance(ts, int) else ""
        t = e.get("tournament") or {}
        uniq = t.get("uniqueTournament") or {}
        comp = uniq.get("name") or t.get("name") or ""
        rnd = (e.get("roundInfo") or {}).get("round")
        rows.append(Row([f"{home} - {away}", when, str(comp), str(rnd or "")]))
    return rows


def build_motorsport_rows(parent_stage_id: int) -> list[Row]:
    subs = stage_substages(parent_stage_id)
    now = dt.datetime.now(tz=ROME)
    rows: list[Row] = []
    for s in subs:
        name = s.get("name") or s.get("description") or "Session"
        typ = (s.get("type") or {}).get("name") or ""
        ts = s.get("startDateTimestamp")
        when = human_when(int(ts), now=now) if isinstance(ts, int) else ""
        rows.append(Row([str(name), str(typ), when]))
    return rows


# ----------------------- news -----------------------


def strip_html(s: str) -> str:
    s = unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def rss_items(url: str, limit: int = 50) -> list[NewsItem]:
    xml = http_text(url, headers={"User-Agent": "OpenClaw"})
    root = ET.fromstring(xml)
    items: list[NewsItem] = []
    for it in root.findall(".//item")[:limit]:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        items.append(NewsItem(title=strip_html(title), link=link, summary=strip_html(desc)))
    return items


def pick_news(items: list[NewsItem], keywords: list[str] | None = None, max_n: int = 4) -> list[NewsItem]:
    if max_n <= 0:
        return []
    if not keywords:
        return items[:max_n]
    kws = [k.lower() for k in keywords]

    def ok(i: NewsItem) -> bool:
        hay = (i.title + " " + i.summary).lower()
        return any(k in hay for k in kws)

    out = [i for i in items if ok(i)]
    return out[:max_n]


def formulapassion_f1_news(max_n: int = 4) -> list[NewsItem]:
    html = http_text(F1_NEWS_FP, headers={"User-Agent": "OpenClaw"})
    # Very lightweight extraction: look for links under formulapassion.it containing /f1/
    found: list[NewsItem] = []
    for m in re.finditer(r"<a[^>]+href=\"(https?://www\.formulapassion\.it/[^\"]+)\"[^>]*>(.*?)</a>", html, re.I | re.S):
        link = m.group(1)
        title = strip_html(m.group(2))
        if not title or "cookie" in title.lower():
            continue
        if "/f1/" not in link:
            continue
        found.append(NewsItem(title=title, link=link, summary=""))
        if len(found) >= max_n:
            break
    return found


# ----------------------- Notion idempotent write -----------------------


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


# ----------------------- main -----------------------


def section_with_news(title: str, table_header: list[str], table_rows: list[Row], news: list[NewsItem]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [heading(title)]
    blocks.append(table_block(table_rows, table_header))

    if news:
        blocks.append(heading3("Top news"))
        for n in news:
            # one recap paragraph + full link
            txt = n.title
            parts = rt(txt, link=n.link)
            if n.summary:
                parts.append({"type": "text", "text": {"content": f" — {n.summary}"}})
            blocks.append(paragraph(parts))
    return blocks


def main() -> int:
    token = open(NOTION_TOKEN_PATH, "r", encoding="utf-8").read().strip()
    if not token:
        print("Notion token file is empty.", file=sys.stderr)
        return 2

    d = today_rome()
    page_title = f"Riepilogo Sportivo Giornaliero del {d.isoformat()}"

    # News pulls
    serie_a_items = rss_items(RSS_SERIE_A)
    tennis_items = rss_items(RSS_TENNIS)
    motogp_items = rss_items(RSS_MOTOGP)
    f1_items = rss_items(RSS_F1)

    fio_news = pick_news(serie_a_items, ["fiorentina"], 4)
    milan_news = pick_news(serie_a_items, ["milan"], 4)
    sinner_news = pick_news(tennis_items, ["sinner"], 4)
    motogp_news = pick_news(motogp_items, None, 4)
    f1_news = pick_news(f1_items, None, 4)
    # Add extra F1 source
    try:
        f1_news_extra = formulapassion_f1_news(4)
    except Exception:
        f1_news_extra = []

    # Events
    fio_rows = build_match_rows(next_team_events(FIORENTINA_ID, 2))
    milan_rows = build_match_rows(next_team_events(MILAN_ID, 2))

    sinner_ev = sinner_events_fallback(2)
    sinner_rows = build_match_rows(sinner_ev) if sinner_ev else [Row(["No upcoming match found via API (fallback)", "", "", ""])]

    vm_ev = volley_next_major_events(IT_VOLLEY_M_ID, 2)
    vf_ev = volley_next_major_events(IT_VOLLEY_F_ID, 2)
    vm_rows = build_match_rows(vm_ev) if vm_ev else [Row(["No major events found (VNL/Euros/Worlds)", "", "", ""])]
    vf_rows = build_match_rows(vf_ev) if vf_ev else [Row(["No major events found (VNL/Euros/Worlds)", "", "", ""])]

    f1_stage = find_next_stage_id(F1_UNIQUE_STAGE_ID)
    motogp_stage = find_next_stage_id(MOTOGP_UNIQUE_STAGE_ID)

    if f1_stage:
        f1_parent = stage_summary(f1_stage)
        f1_sessions = build_motorsport_rows(f1_stage)
        # Prepend GP name/location paragraph
        f1_title = f"Formula 1 — {f1_parent['name']}"
        f1_rows = f1_sessions[:25]  # cap
    else:
        f1_title = "Formula 1"
        f1_rows = [Row(["Not found", "", ""]) ]

    if motogp_stage:
        mg_parent = stage_summary(motogp_stage)
        mg_sessions = build_motorsport_rows(motogp_stage)
        mg_title = f"MotoGP — {mg_parent['name']}"
        mg_rows = mg_sessions[:25]
    else:
        mg_title = "MotoGP"
        mg_rows = [Row(["Not found", "", ""]) ]

    # Build Notion content
    children: list[dict[str, Any]] = [
        paragraph("Daily sports briefing (auto)."),
    ]

    children += section_with_news(
        "Fiorentina",
        ["Match", "When", "Competition", "Round"],
        fio_rows,
        fio_news,
    )
    children += section_with_news(
        "AC Milan",
        ["Match", "When", "Competition", "Round"],
        milan_rows,
        milan_news,
    )
    children += section_with_news(
        "Jannik Sinner",
        ["Match", "When", "Tournament", "Round"],
        sinner_rows,
        sinner_news,
    )
    children += section_with_news(
        "Italia Volley (Men)",
        ["Match", "When", "Competition", "Round"],
        vm_rows,
        [],
    )
    children += section_with_news(
        "Italia Volley (Women)",
        ["Match", "When", "Competition", "Round"],
        vf_rows,
        [],
    )
    children += section_with_news(
        f"MotoGP (next weekend sessions)",
        ["Session", "Type", "When"],
        mg_rows,
        motogp_news,
    )

    # F1: merge news sources (gazzetta + formulapassion)
    merged_f1_news = (f1_news + f1_news_extra)[:4]
    children += section_with_news(
        f"Formula 1 (next weekend sessions)",
        ["Session", "Type", "When"],
        f1_rows,
        merged_f1_news,
    )

    page = notion_find_page_by_title(token, page_title)
    if page is None:
        page = notion_create_page(token, page_title)
    notion_replace_children(token, page["id"], children)

    print(page_title)
    print("Updated Notion page:", page.get("url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Gather deterministic data for the daily sport briefing.

Produces two JSON files in a single run:
  events.json  — formatted event tables (Sofascore), ready for the briefing
  news.json    — raw RSS/web news candidates for LLM selection

Usage:
  python3 gather_payload.py
  python3 gather_payload.py --events-out /path/events.json --news-out /path/news.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from html import unescape
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

ROME = ZoneInfo("Europe/Rome")
UTC = dt.timezone.utc

REPORTS_DIR = os.path.expanduser(
    "~/.openclaw/workspace/reports/sport-briefing"
)

# ---- Sofascore API ----
SOFASCORE = "https://api.sofascore.com/api/v1"

FIORENTINA_ID = 2693
MILAN_ID = 2692
SINNER_TEAM_ID = 206570
IT_VOLLEY_M_ID = 6824
IT_VOLLEY_F_ID = 6709
F1_UNIQUE_STAGE_ID = 40
MOTOGP_UNIQUE_STAGE_ID = 17

# ---- RSS feeds ----
RSS_SERIE_A = "https://www.gazzetta.it/dynamic-feed/rss/section/Calcio/Serie-A.xml"
RSS_TENNIS = "https://www.gazzetta.it/dynamic-feed/rss/section/Tennis.xml"
RSS_MOTOGP = "https://www.gazzetta.it/dynamic-feed/rss/section/Moto/moto-GP.xml"
RSS_F1 = "https://www.gazzetta.it/dynamic-feed/rss/section/Formula-1.xml"
RSS_VOLLEY = "https://www.gazzetta.it/dynamic-feed/rss/section/Volley.xml"
F1_NEWS_FP = "https://www.formulapassion.it/f1/f1-news"

VOLLEY_MAJOR_PATTERNS = [
    re.compile(r"VNL|Nations League", re.I),
    re.compile(r"World Championship|World Cup", re.I),
    re.compile(r"European Championship|Euro", re.I),
]

# ---- Date formatting labels ----
EN_WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EN_WEEKDAY_FULL = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]
EN_MONTH = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


@dataclass
class NewsItem:
    title: str
    link: str
    summary: str = ""
    source: str = ""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def http_text(url: str, headers: Optional[dict[str, str]] = None) -> str:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "OpenClaw"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def http_json(url: str) -> Any:
    return json.loads(http_text(url))


def strip_html(s: str) -> str:
    s = unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Date / time formatting
# ---------------------------------------------------------------------------


def human_when(ts: int, now: Optional[dt.datetime] = None) -> str:
    """Convert a UNIX timestamp to a human-friendly relative string.

    Examples: "Today at 20:45 (Sat 28.Feb)", "Next Monday at 20:45 (Mon 02.Mar)",
              "In 8 days at 15:00 (Sun 08.Mar)", "In 14 weeks at 22:00 (Wed 10.Jun)"
    """
    now = now or dt.datetime.now(tz=ROME)
    d = dt.datetime.fromtimestamp(ts, tz=ROME)
    days = (d.date() - now.date()).days
    hhmm = d.strftime("%H:%M")
    suffix = f"({EN_WEEKDAY[d.weekday()]} {d.day:02d}.{EN_MONTH[d.month - 1]})"

    if days == -1:
        return f"Yesterday at {hhmm} {suffix}"
    if days < -1:
        return f"Last {EN_WEEKDAY_FULL[d.weekday()]} at {hhmm}"
    if days == 0:
        return f"Today at {hhmm} {suffix}"
    if days == 1:
        return f"Tomorrow at {hhmm} {suffix}"
    if 2 <= days <= 7:
        return f"Next {EN_WEEKDAY_FULL[d.weekday()]} at {hhmm} {suffix}"
    if days >= 14:
        return f"In {days // 7} weeks at {hhmm} {suffix}"
    return f"In {days} days at {hhmm} {suffix}"


# ---------------------------------------------------------------------------
# RSS / web news fetchers
# ---------------------------------------------------------------------------


def rss_items(url: str, source: str, limit: int = 60) -> list[NewsItem]:
    xml = http_text(url)
    root = ET.fromstring(xml)
    out: list[NewsItem] = []
    for it in root.findall(".//item")[:limit]:
        title = strip_html((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        desc = strip_html((it.findtext("description") or "").strip())
        if title and link:
            out.append(NewsItem(title=title, link=link, summary=desc, source=source))
    return out


def formulapassion_links(max_n: int = 40) -> list[NewsItem]:
    html = http_text(F1_NEWS_FP)
    found: list[NewsItem] = []
    for m in re.finditer(
        r"<a[^>]+href=\"(https?://www\.formulapassion\.it/[^\"]+)\"[^>]*>(.*?)</a>",
        html, re.I | re.S,
    ):
        link = m.group(1)
        title = strip_html(m.group(2))
        if not title or "/f1/" not in link:
            continue
        found.append(NewsItem(title=title, link=link, summary="", source="formulapassion"))
        if len(found) >= max_n:
            break
    return found


# ---------------------------------------------------------------------------
# Sofascore fetchers
# ---------------------------------------------------------------------------


def next_team_events(team_id: int, limit: int = 2) -> list[dict[str, Any]]:
    data = http_json(f"{SOFASCORE}/team/{team_id}/events/next/0")
    ev = sorted(data.get("events", []), key=lambda e: e.get("startTimestamp", 10**18))
    return ev[:limit]


def sinner_events_fallback(limit: int = 2) -> list[dict[str, Any]]:
    data = http_json(f"{SOFASCORE}/team/{SINNER_TEAM_ID}/events/last/0")
    ev = data.get("events", [])
    now_ts = int(dt.datetime.now(tz=UTC).timestamp())
    upcoming = [
        e for e in ev
        if e.get("startTimestamp", 0) >= now_ts
        and ((e.get("status") or {}).get("type") or "") in {"notstarted", "inprogress"}
    ]
    return sorted(upcoming, key=lambda e: e.get("startTimestamp", 10**18))[:limit]


def volley_next_major_events(team_id: int, limit: int = 2) -> list[dict[str, Any]]:
    data = http_json(f"{SOFASCORE}/team/{team_id}/events/next/0")
    ev = data.get("events", [])

    def is_major(e: dict[str, Any]) -> bool:
        t = e.get("tournament") or {}
        uniq = t.get("uniqueTournament") or {}
        ut_name = uniq.get("name") or t.get("name") or ""
        cat = (t.get("category") or {}).get("name") or (uniq.get("category") or {}).get("name")
        if (cat or "").lower() != "international":
            return False
        return any(p.search(ut_name) for p in VOLLEY_MAJOR_PATTERNS)

    return sorted(
        [e for e in ev if is_major(e)],
        key=lambda e: e.get("startTimestamp", 10**18),
    )[:limit]


def find_current_or_next_stage(unique_stage_id: int) -> tuple[Optional[int], str]:
    """Find the current in-progress or next upcoming F1/MotoGP stage.

    Returns (stage_id, status) where status is "inprogress" or "notstarted".
    """
    q = "formula 1" if unique_stage_id == F1_UNIQUE_STAGE_ID else "motogp"
    data = http_json(f"{SOFASCORE}/search/all?q={urllib.request.quote(q)}")
    now_ts = int(dt.datetime.now(tz=UTC).timestamp())
    week_ago_ts = now_ts - 7 * 86400

    candidates: list[dict[str, Any]] = []
    for r in data.get("results", []):
        if r.get("type") != "stage":
            continue
        ent = r.get("entity") or {}
        cat_l = ((ent.get("category") or {}).get("name") or "").lower()
        if unique_stage_id == F1_UNIQUE_STAGE_ID and "formula" not in cat_l:
            continue
        if unique_stage_id == MOTOGP_UNIQUE_STAGE_ID and "motogp" not in cat_l:
            continue
        st = ent.get("startTimestamp")
        if not isinstance(st, int) or st < week_ago_ts:
            continue
        candidates.append(ent)

    candidates.sort(key=lambda e: e.get("startTimestamp", 10**18))

    for c in candidates:
        if c.get("startTimestamp", 0) >= now_ts:
            break
        sid = int(c["id"])
        detail = http_json(f"{SOFASCORE}/stage/{sid}")
        status = ((detail.get("stage") or {}).get("status") or {}).get("type") or ""
        if status == "inprogress":
            return sid, "inprogress"

    for c in candidates:
        if c.get("startTimestamp", 0) >= now_ts:
            return int(c["id"]), "notstarted"

    return None, ""


def stage_substages(stage_id: int) -> list[dict[str, Any]]:
    data = http_json(f"https://www.sofascore.com/api/v1/stage/{stage_id}/substages")
    return sorted(data.get("stages", []), key=lambda s: s.get("startDateTimestamp", 10**18))


# ---------------------------------------------------------------------------
# Row builders — format Sofascore events into table rows
# ---------------------------------------------------------------------------


def build_match_rows(events: list[dict[str, Any]], now: dt.datetime) -> list[list[str]]:
    rows: list[list[str]] = []
    for e in events:
        home = (e.get("homeTeam") or {}).get("name") or "?"
        away = (e.get("awayTeam") or {}).get("name") or "?"
        ts = e.get("startTimestamp")
        when = human_when(int(ts), now=now) if isinstance(ts, int) else ""
        t = e.get("tournament") or {}
        uniq = t.get("uniqueTournament") or {}
        comp = uniq.get("name") or t.get("name") or ""
        rnd = (e.get("roundInfo") or {}).get("round")
        rows.append([f"{home} - {away}", when, str(comp), str(rnd or "")])
    return rows


def build_motorsport_rows(parent_stage_id: int, now: dt.datetime) -> list[list[str]]:
    rows: list[list[str]] = []
    for s in stage_substages(parent_stage_id):
        name = s.get("name") or s.get("description") or "Session"
        typ = (s.get("type") or {}).get("name") or ""
        ts = s.get("startDateTimestamp")
        when = human_when(int(ts), now=now) if isinstance(ts, int) else ""
        rows.append([str(name), str(typ), when])
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Gather events + news for the daily sport briefing.",
    )
    ap.add_argument(
        "--events-out",
        default=os.path.join(REPORTS_DIR, "events.json"),
        help="Output path for formatted events (default: %(default)s)",
    )
    ap.add_argument(
        "--news-out",
        default=os.path.join(REPORTS_DIR, "news.json"),
        help="Output path for news candidates (default: %(default)s)",
    )
    args = ap.parse_args()

    now = dt.datetime.now(tz=ROME)
    d = now.date()
    page_title = f"Riepilogo Sportivo Giornaliero del {d.isoformat()}"

    # ── Events ──────────────────────────────────────────────────────────────

    fio_rows = build_match_rows(next_team_events(FIORENTINA_ID), now)
    milan_rows = build_match_rows(next_team_events(MILAN_ID), now)

    sinner_ev = sinner_events_fallback()
    sinner_rows = (
        build_match_rows(sinner_ev, now) if sinner_ev
        else [["No upcoming match found via API (fallback)", "", "", ""]]
    )

    vm_ev = volley_next_major_events(IT_VOLLEY_M_ID)
    vf_ev = volley_next_major_events(IT_VOLLEY_F_ID)
    vm_rows = (
        build_match_rows(vm_ev, now) if vm_ev
        else [["No major events found (VNL/Euros/Worlds)", "", "", ""]]
    )
    vf_rows = (
        build_match_rows(vf_ev, now) if vf_ev
        else [["No major events found (VNL/Euros/Worlds)", "", "", ""]]
    )

    f1_stage, f1_status = find_current_or_next_stage(F1_UNIQUE_STAGE_ID)
    mg_stage, mg_status = find_current_or_next_stage(MOTOGP_UNIQUE_STAGE_ID)

    def motor_title(sport: str, status: str) -> str:
        tag = "current weekend sessions" if status == "inprogress" else "next weekend sessions"
        return f"{sport} ({tag})"

    f1_rows = build_motorsport_rows(f1_stage, now) if f1_stage else [["Not found", "", ""]]
    mg_rows = build_motorsport_rows(mg_stage, now) if mg_stage else [["Not found", "", ""]]

    events_data = {
        "date": d.isoformat(),
        "pageTitle": page_title,
        "intro": "Daily sports briefing (auto).",
        "sections": [
            {"title": "Fiorentina", "table": {"header": ["Match", "When", "Competition", "Round"], "rows": fio_rows}},
            {"title": "AC Milan", "table": {"header": ["Match", "When", "Competition", "Round"], "rows": milan_rows}},
            {"title": "Jannik Sinner", "table": {"header": ["Match", "When", "Tournament", "Round"], "rows": sinner_rows}},
            {"title": "Italia Volley (Men)", "table": {"header": ["Match", "When", "Competition", "Round"], "rows": vm_rows}},
            {"title": "Italia Volley (Women)", "table": {"header": ["Match", "When", "Competition", "Round"], "rows": vf_rows}},
            {"title": motor_title("MotoGP", mg_status), "table": {"header": ["Session", "Type", "When"], "rows": mg_rows}},
            {"title": motor_title("Formula 1", f1_status), "table": {"header": ["Session", "Type", "When"], "rows": f1_rows}},
        ],
    }

    # ── News candidates ─────────────────────────────────────────────────────

    serie_a = [asdict(x) for x in rss_items(RSS_SERIE_A, "gazzetta-serie-a")]
    tennis = [asdict(x) for x in rss_items(RSS_TENNIS, "gazzetta-tennis")]
    volley_news = [asdict(x) for x in rss_items(RSS_VOLLEY, "gazzetta-volley")]
    motogp_news = [asdict(x) for x in rss_items(RSS_MOTOGP, "gazzetta-motogp")]
    f1_gazzetta = [asdict(x) for x in rss_items(RSS_F1, "gazzetta-f1")]
    f1_fp = [asdict(x) for x in formulapassion_links()]

    news_data = {
        "date": d.isoformat(),
        "candidates": {
            "Fiorentina": serie_a,
            "AC Milan": serie_a,
            "Jannik Sinner": tennis,
            "Italia Volley (Men)": volley_news,
            "Italia Volley (Women)": volley_news,
            "MotoGP": motogp_news,
            "Formula 1": f1_gazzetta + f1_fp,
        },
    }

    # ── Write outputs ────────────────────────────────────────────────────────

    for path, data in [(args.events_out, events_data), (args.news_out, news_data)]:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"Written: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

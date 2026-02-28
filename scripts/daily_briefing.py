#!/usr/bin/env python3
"""Fetch upcoming sports events from Sofascore and save a briefing JSON file.

This script is step 1 of the daily briefing pipeline:
  1. daily_briefing.py  → fetches live data, writes briefing.json
  2. save.py            → reads briefing.json, writes/updates a Notion page

It queries Sofascore for 7 sport sections (Fiorentina, AC Milan, Sinner,
Italy Volleyball M/F, MotoGP, Formula 1) and formats each event into a
human-friendly table row with dates localised to Europe/Rome.

Output: a JSON file with the structure:
  {
    "pageTitle": "Riepilogo Sportivo Giornaliero del YYYY-MM-DD",
    "intro": "...",
    "sections": [
      {
        "title": "Section Name",
        "table": { "header": [...], "rows": [[...], ...] },
        "news": []
      }, ...
    ]
  }

Usage:
  python3 daily_briefing.py [--out /path/to/briefing.json]

If --out is omitted the default path is:
  ~/.openclaw/workspace/reports/daily-briefing/briefing.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import urllib.request
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python < 3.9 compatibility
    from backports.zoneinfo import ZoneInfo  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All timestamps are converted to Rome local time before formatting.
ROME = ZoneInfo("Europe/Rome")
UTC = dt.timezone.utc

# Default output path for the briefing JSON file.
DEFAULT_OUT = os.path.expanduser(
    "~/.openclaw/workspace/reports/daily-briefing/briefing.json"
)

# ---- Sofascore API ----
SOFASCORE = "https://api.sofascore.com/api/v1"

# Team / player entity IDs on Sofascore.
FIORENTINA_ID = 2693
MILAN_ID = 2692
SINNER_TEAM_ID = 206570  # Sofascore treats tennis players as "teams"
IT_VOLLEY_M_ID = 6824
IT_VOLLEY_F_ID = 6709

# "Unique stage" IDs used to discover the next race weekend via search.
F1_UNIQUE_STAGE_ID = 40
MOTOGP_UNIQUE_STAGE_ID = 17

# Volleyball: only include matches from major international tournaments.
# Friendlies and minor cups are filtered out.
VOLLEY_MAJOR_PATTERNS = [
    re.compile(r"VNL|Nations League", re.I),
    re.compile(r"World Championship|World Cup", re.I),
    re.compile(r"European Championship|Euro", re.I),
]

# ---------------------------------------------------------------------------
# Weekday / month labels (English abbreviations used in the "When" column)
# ---------------------------------------------------------------------------
EN_WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EN_WEEKDAY_FULL = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]
EN_MONTH = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no requests / httpx)
# ---------------------------------------------------------------------------


def http_text(url: str, headers: Optional[dict[str, str]] = None) -> str:
    """GET *url* and return the response body as a string."""
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def http_json(url: str, headers: Optional[dict[str, str]] = None) -> Any:
    """GET *url*, parse the response as JSON and return the result."""
    return json.loads(http_text(url, headers=headers))


# ---------------------------------------------------------------------------
# Date / time formatting
# ---------------------------------------------------------------------------


def today_rome() -> dt.date:
    """Return today's date in the Europe/Rome timezone."""
    return dt.datetime.now(tz=ROME).date()


def human_when(ts: int, now: Optional[dt.datetime] = None) -> str:
    """Convert a UNIX timestamp to a human-friendly relative string.

    Examples of output:
      "Yesterday at 09:00 (Fri 27.Feb)"
      "Today at 20:45 (Sat 28.Feb)"
      "Tomorrow at 15:00 (Sun 01.Mar)"
      "Next Monday at 20:45 (Mon 02.Mar)"
      "In 8 days at 15:00 (Sun 08.Mar)"
      "In 14 weeks at 22:00 (Wed 10.Jun)"
    """
    now = now or dt.datetime.now(tz=ROME)
    d = dt.datetime.fromtimestamp(ts, tz=ROME)
    days = (d.date() - now.date()).days
    hhmm = d.strftime("%H:%M")
    # Compact date suffix — e.g. "(Mon 02.Mar)"
    suffix = f"({EN_WEEKDAY[d.weekday()]} {d.day:02d}.{EN_MONTH[d.month - 1]})"

    # Past dates
    if days == -1:
        return f"Yesterday at {hhmm} {suffix}"
    if days < -1:
        return f"Last {EN_WEEKDAY_FULL[d.weekday()]} at {hhmm}"

    if days == 0:
        return f"Today at {hhmm} {suffix}"
    if days == 1:
        return f"Tomorrow at {hhmm} {suffix}"
    if 2 <= days <= 7:
        # "Next Wednesday at …"
        return f"Next {EN_WEEKDAY_FULL[d.weekday()]} at {hhmm} {suffix}"
    if days >= 14:
        weeks = days // 7
        return f"In {weeks} weeks at {hhmm} {suffix}"
    # 8–13 days
    return f"In {days} days at {hhmm} {suffix}"


# ---------------------------------------------------------------------------
# Sofascore fetchers
# ---------------------------------------------------------------------------


def next_team_events(team_id: int, limit: int = 2) -> list[dict[str, Any]]:
    """Return the next *limit* scheduled events for a team (football, volleyball, …)."""
    data = http_json(f"{SOFASCORE}/team/{team_id}/events/next/0")
    ev = sorted(
        data.get("events", []),
        key=lambda e: e.get("startTimestamp", 10**18),
    )
    return ev[:limit]


def sinner_events_fallback(limit: int = 2) -> list[dict[str, Any]]:
    """Return upcoming Sinner matches using the /events/last fallback.

    The Sofascore /events/next endpoint is unreliable for individual tennis
    players, so we fetch recent events and filter for those that haven't
    started yet (status "notstarted" or "inprogress").
    """
    data = http_json(f"{SOFASCORE}/team/{SINNER_TEAM_ID}/events/last/0")
    ev = data.get("events", [])
    now_ts = int(dt.datetime.now(tz=UTC).timestamp())

    def status_type(e: dict[str, Any]) -> str:
        return (e.get("status") or {}).get("type") or ""

    upcoming = [
        e for e in ev
        if e.get("startTimestamp", 0) >= now_ts
        and status_type(e) in {"notstarted", "inprogress"}
    ]
    return sorted(upcoming, key=lambda e: e.get("startTimestamp", 10**18))[:limit]


def volley_next_major_events(team_id: int, limit: int = 2) -> list[dict[str, Any]]:
    """Return next volleyball events, filtered to major international tournaments only.

    Minor cups, friendlies, and domestic leagues are excluded via
    VOLLEY_MAJOR_PATTERNS regex checks on the tournament name.
    """
    data = http_json(f"{SOFASCORE}/team/{team_id}/events/next/0")
    ev = data.get("events", [])

    def is_major(e: dict[str, Any]) -> bool:
        t = e.get("tournament") or {}
        uniq = t.get("uniqueTournament") or {}
        ut_name = uniq.get("name") or t.get("name") or ""
        cat = (
            (t.get("category") or {}).get("name")
            or (uniq.get("category") or {}).get("name")
        )
        # Must be an international-category tournament…
        if (cat or "").lower() != "international":
            return False
        # …whose name matches one of the major-competition patterns.
        return any(p.search(ut_name) for p in VOLLEY_MAJOR_PATTERNS)

    ev = [e for e in ev if is_major(e)]
    return sorted(ev, key=lambda e: e.get("startTimestamp", 10**18))[:limit]


def find_current_or_next_stage(unique_stage_id: int) -> tuple[Optional[int], str]:
    """Find the current in-progress or next upcoming F1/MotoGP stage.

    Returns (stage_id, status) where status is "inprogress" or "notstarted".
    The search API doesn't include status, so we widen the time window to
    include stages that started up to 7 days ago (a race weekend's max span),
    then check each candidate's detailed status via /stage/{id}.

    An in-progress stage is always preferred over the next upcoming one.
    """
    q = "formula 1" if unique_stage_id == F1_UNIQUE_STAGE_ID else "motogp"
    data = http_json(f"{SOFASCORE}/search/all?q={urllib.request.quote(q)}")
    now_ts = int(dt.datetime.now(tz=UTC).timestamp())
    # Include stages that started up to 7 days ago (may still be in progress).
    week_ago_ts = now_ts - 7 * 86400

    candidates: list[dict[str, Any]] = []
    for r in data.get("results", []):
        if r.get("type") != "stage":
            continue
        ent = r.get("entity") or {}
        cat = (ent.get("category") or {}).get("name") or ""
        cat_l = cat.lower()
        # Ensure we only pick the right motorsport category.
        if unique_stage_id == F1_UNIQUE_STAGE_ID and "formula" not in cat_l:
            continue
        if unique_stage_id == MOTOGP_UNIQUE_STAGE_ID and "motogp" not in cat_l:
            continue
        st = ent.get("startTimestamp")
        if not isinstance(st, int) or st < week_ago_ts:
            continue
        candidates.append(ent)

    # Sort chronologically — earliest first.
    candidates = sorted(candidates, key=lambda e: e.get("startTimestamp", 10**18))

    # Check for an in-progress stage first (its startTimestamp is in the past).
    for c in candidates:
        if c.get("startTimestamp", 0) >= now_ts:
            break  # all remaining are future; stop checking
        stage_id = int(c["id"])
        detail = http_json(f"{SOFASCORE}/stage/{stage_id}")
        status = ((detail.get("stage") or {}).get("status") or {}).get("type") or ""
        if status == "inprogress":
            return stage_id, "inprogress"

    # No in-progress stage found — pick the next upcoming one.
    for c in candidates:
        if c.get("startTimestamp", 0) >= now_ts:
            return int(c["id"]), "notstarted"

    return None, ""


def stage_summary(stage_id: int) -> dict[str, str]:
    """Fetch metadata for a motorsport stage (GP name, country, circuit, city)."""
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
    """Fetch individual sessions (substages) for a motorsport stage.

    Each substage represents a practice, qualifying, sprint, or race session.
    Results are sorted chronologically by start time.
    """
    data = http_json(f"https://www.sofascore.com/api/v1/stage/{stage_id}/substages")
    stages = data.get("stages", [])
    return sorted(stages, key=lambda s: s.get("startDateTimestamp", 10**18))


# ---------------------------------------------------------------------------
# Row builders — convert raw Sofascore events into table-row lists
# ---------------------------------------------------------------------------


def build_match_rows(events: list[dict[str, Any]]) -> list[list[str]]:
    """Build table rows for football / tennis / volleyball events.

    Each row is a list of strings: [match, when, competition, round].
    """
    rows: list[list[str]] = []
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
        rows.append([f"{home} - {away}", when, str(comp), str(rnd or "")])
    return rows


def build_motorsport_rows(parent_stage_id: int) -> list[list[str]]:
    """Build table rows for motorsport sessions (practice, quali, race, …).

    Each row is a list of strings: [session_name, type, when].
    """
    subs = stage_substages(parent_stage_id)
    now = dt.datetime.now(tz=ROME)
    rows: list[list[str]] = []
    for s in subs:
        name = s.get("name") or s.get("description") or "Session"
        typ = (s.get("type") or {}).get("name") or ""
        ts = s.get("startDateTimestamp")
        when = human_when(int(ts), now=now) if isinstance(ts, int) else ""
        rows.append([str(name), str(typ), when])
    return rows


# ---------------------------------------------------------------------------
# Main — fetch all events and write briefing.json
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch sports events from Sofascore and produce briefing.json",
    )
    ap.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help="Path to write the output JSON (default: %(default)s)",
    )
    args = ap.parse_args()

    d = today_rome()
    page_title = f"Riepilogo Sportivo Giornaliero del {d.isoformat()}"

    # ---- Fetch events for each sport section ----

    fio_rows = build_match_rows(next_team_events(FIORENTINA_ID, 2))
    milan_rows = build_match_rows(next_team_events(MILAN_ID, 2))

    sinner_ev = sinner_events_fallback(2)
    sinner_rows = (
        build_match_rows(sinner_ev) if sinner_ev
        else [["No upcoming match found via API (fallback)", "", "", ""]]
    )

    vm_ev = volley_next_major_events(IT_VOLLEY_M_ID, 2)
    vf_ev = volley_next_major_events(IT_VOLLEY_F_ID, 2)
    vm_rows = (
        build_match_rows(vm_ev) if vm_ev
        else [["No major events found (VNL/Euros/Worlds)", "", "", ""]]
    )
    vf_rows = (
        build_match_rows(vf_ev) if vf_ev
        else [["No major events found (VNL/Euros/Worlds)", "", "", ""]]
    )

    # ---- Motorsport: discover current or next race weekend, then fetch sessions ----

    f1_stage, f1_status = find_current_or_next_stage(F1_UNIQUE_STAGE_ID)
    motogp_stage, mg_status = find_current_or_next_stage(MOTOGP_UNIQUE_STAGE_ID)

    def _motorsport_title(sport: str, status: str) -> str:
        """Use 'current weekend sessions' if the stage is live, else 'next'."""
        label = "current weekend sessions" if status == "inprogress" else "next weekend sessions"
        return "%s (%s)" % (sport, label)

    if f1_stage:
        f1_sessions = build_motorsport_rows(f1_stage)
        f1_rows = f1_sessions[:25]  # safety cap
    else:
        f1_rows = [["Not found", "", ""]]

    if motogp_stage:
        mg_sessions = build_motorsport_rows(motogp_stage)
        mg_rows = mg_sessions[:25]
    else:
        mg_rows = [["Not found", "", ""]]

    # ---- Assemble the briefing JSON ----
    # The schema matches what save.py (and write_notion_from_briefing.py) expect:
    #   sections[].table.header  — column names
    #   sections[].table.rows    — list of row lists
    #   sections[].news          — empty here; filled by the LLM step when used

    sections = [
        {
            "title": "Fiorentina",
            "table": {"header": ["Match", "When", "Competition", "Round"], "rows": fio_rows},
            "news": [],
        },
        {
            "title": "AC Milan",
            "table": {"header": ["Match", "When", "Competition", "Round"], "rows": milan_rows},
            "news": [],
        },
        {
            "title": "Jannik Sinner",
            "table": {"header": ["Match", "When", "Tournament", "Round"], "rows": sinner_rows},
            "news": [],
        },
        {
            "title": "Italia Volley (Men)",
            "table": {"header": ["Match", "When", "Competition", "Round"], "rows": vm_rows},
            "news": [],
        },
        {
            "title": "Italia Volley (Women)",
            "table": {"header": ["Match", "When", "Competition", "Round"], "rows": vf_rows},
            "news": [],
        },
        {
            "title": _motorsport_title("MotoGP", mg_status),
            "table": {"header": ["Session", "Type", "When"], "rows": mg_rows},
            "news": [],
        },
        {
            "title": _motorsport_title("Formula 1", f1_status),
            "table": {"header": ["Session", "Type", "When"], "rows": f1_rows},
            "news": [],
        },
    ]

    briefing = {
        "pageTitle": page_title,
        "intro": "Riepilogo automatico (eventi + notizie selezionate).",
        "sections": sections,
    }

    # ---- Write output file ----

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(briefing, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Briefing written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

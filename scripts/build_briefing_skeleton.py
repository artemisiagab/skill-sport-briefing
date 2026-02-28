#!/usr/bin/env python3
"""Build a Notion-briefing skeleton JSON from the gathered payload.

This produces a *deterministic* briefing.json with:
- correct page title (Riepilogo Sportivo Giornaliero del YYYY-MM-DD)
- fixed sections + tables (events)
- per-section candidate news lists (unfiltered)

The LLM step should then:
- choose 0–4 news items per section (or 0 if nothing relevant)
- add an Italian 1-paragraph recap per selected item

Usage:
  python3 build_briefing_skeleton.py --payload payload.json --out briefing.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

ROME = ZoneInfo("Europe/Rome")
EN_WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EN_WEEKDAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
EN_MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def human_when(ts: int, now: dt.datetime) -> str:
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


def football_rows(events: list[dict[str, Any]], now: dt.datetime) -> list[list[str]]:
    out: list[list[str]] = []
    for e in events[:2]:
        home = (e.get("homeTeam") or {}).get("name") or "?"
        away = (e.get("awayTeam") or {}).get("name") or "?"
        ts = int(e.get("startTimestamp") or 0)
        when = human_when(ts, now) if ts else ""
        t = e.get("tournament") or {}
        uniq = t.get("uniqueTournament") or {}
        comp = uniq.get("name") or t.get("name") or ""
        rnd = (e.get("roundInfo") or {}).get("round")
        out.append([f"{home} - {away}", when, str(comp), str(rnd or "")])
    return out


def tennis_rows(events: list[dict[str, Any]], now: dt.datetime) -> list[list[str]]:
    # same schema as football for our tables
    return football_rows(events, now)


def volley_rows(events: list[dict[str, Any]], now: dt.datetime) -> list[list[str]]:
    return football_rows(events, now)


def motorsport_rows(substages: list[dict[str, Any]], now: dt.datetime, status: str = "") -> list[list[str]]:
    """Return session rows.

    Rule:
    - If stage is inprogress: show ALL sessions.
    - If not inprogress: show the upcoming weekend sessions (also all sessions available).

    (We rely on gather_payload selecting the right stageId: current inprogress else next notstarted.)
    """
    out: list[list[str]] = []

    for s in substages:
        ts = int(s.get("startDateTimestamp") or 0)
        if not ts:
            continue
        name = s.get("name") or s.get("description") or "Session"
        typ = (s.get("type") or {}).get("name") or ""
        when = human_when(ts, now)
        out.append([str(name), str(typ), when])
        if len(out) >= 30:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    payload = json.loads(open(args.payload, "r", encoding="utf-8").read())
    date = payload["date"]
    now = dt.datetime.now(tz=ROME)

    # Candidate news pools
    serie_a = payload["news"].get("serie_a", [])
    tennis = payload["news"].get("tennis", [])
    motogp_news = payload["news"].get("motogp", [])
    f1_news = payload["news"].get("f1", [])
    f1_extra = payload["news"].get("f1_extra", [])

    def pick_candidates(items: list[dict[str, Any]], keywords: list[str] | None = None, limit: int = 12) -> list[dict[str, Any]]:
        if not keywords:
            return items[:limit]
        kws = [k.lower() for k in keywords]
        out = []
        for it in items:
            hay = ((it.get("title") or "") + " " + (it.get("summary") or "")).lower()
            if any(k in hay for k in kws):
                out.append(it)
            if len(out) >= limit:
                break
        return out

    fio_candidates = pick_candidates(serie_a, ["fiorentina"], 12)
    milan_candidates = pick_candidates(serie_a, ["milan"], 12)
    sinner_candidates = pick_candidates(tennis, ["sinner"], 12)

    # Tables
    events = payload["events"]

    sections = [
        {
            "title": "Fiorentina",
            "table": {"header": ["Match", "When", "Competition", "Round"], "rows": football_rows(events.get("fiorentina", []), now)},
            "newsCandidates": fio_candidates,
            "news": [],
        },
        {
            "title": "AC Milan",
            "table": {"header": ["Match", "When", "Competition", "Round"], "rows": football_rows(events.get("milan", []), now)},
            "newsCandidates": milan_candidates,
            "news": [],
        },
        {
            "title": "Jannik Sinner",
            "table": {"header": ["Match", "When", "Tournament", "Round"], "rows": tennis_rows(events.get("sinner", []), now)},
            "newsCandidates": sinner_candidates,
            "news": [],
        },
        {
            "title": "Italia Volley (Men)",
            "table": {"header": ["Match", "When", "Competition", "Round"], "rows": volley_rows(events.get("volley_m", []), now)},
            "newsCandidates": [],
            "news": [],
        },
        {
            "title": "Italia Volley (Women)",
            "table": {"header": ["Match", "When", "Competition", "Round"], "rows": volley_rows(events.get("volley_f", []), now)},
            "newsCandidates": [],
            "news": [],
        },
        {
            "title": "MotoGP (sessioni weekend)",
            "table": {
                "header": ["Session", "Type", "When"],
                "rows": motorsport_rows(
                    (events.get("motogp", {}) or {}).get("substages", []),
                    now,
                    status=(events.get("motogp", {}) or {}).get("status", ""),
                ),
            },
            "newsCandidates": motogp_news[:12],
            "news": [],
        },
        {
            "title": "Formula 1 (sessioni weekend)",
            "table": {
                "header": ["Session", "Type", "When"],
                "rows": motorsport_rows(
                    (events.get("f1", {}) or {}).get("substages", []),
                    now,
                    status=(events.get("f1", {}) or {}).get("status", ""),
                ),
            },
            "newsCandidates": (f1_news + f1_extra)[:16],
            "news": [],
        },
    ]

    briefing = {
        "pageTitle": f"Riepilogo Sportivo Giornaliero del {date}",
        "intro": "Riepilogo automatico (eventi + notizie selezionate).",
        "sections": sections,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(briefing, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()

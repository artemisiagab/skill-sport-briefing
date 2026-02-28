#!/usr/bin/env python3
"""Gather deterministic data for the daily briefing.

Outputs a JSON payload to stdout (and optionally to a file).
No LLM summarization here.

Includes:
- upcoming events from Sofascore
- RSS items (title/link/description)

Usage:
  python3 gather_payload.py > payload.json
  python3 gather_payload.py --out /path/to/payload.json
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

SOFASCORE = "https://api.sofascore.com/api/v1"

# Entities
FIORENTINA_ID = 2693
MILAN_ID = 2692
SINNER_TEAM_ID = 206570
IT_VOLLEY_M_ID = 6824
IT_VOLLEY_F_ID = 6709
F1_UNIQUE_STAGE_ID = 40
MOTOGP_UNIQUE_STAGE_ID = 17

# Feeds
RSS_SERIE_A = "https://www.gazzetta.it/dynamic-feed/rss/section/Calcio/Serie-A.xml"
RSS_TENNIS = "https://www.gazzetta.it/dynamic-feed/rss/section/Tennis.xml"
RSS_MOTOGP = "https://www.gazzetta.it/dynamic-feed/rss/section/Moto/moto-GP.xml"
RSS_F1 = "https://www.gazzetta.it/dynamic-feed/rss/section/Formula-1.xml"
F1_NEWS_FP = "https://www.formulapassion.it/f1/f1-news"

VOLLEY_MAJOR_PATTERNS = [
    re.compile(r"VNL|Nations League", re.I),
    re.compile(r"World Championship|World Cup", re.I),
    re.compile(r"European Championship|Euro", re.I),
]


@dataclass
class NewsItem:
    title: str
    link: str
    summary: str = ""
    source: str = ""


def http_text(url: str, headers: Optional[dict[str, str]] = None) -> str:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "OpenClaw"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def http_json(url: str) -> Any:
    return json.loads(http_text(url))


def strip_html(s: str) -> str:
    s = unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


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
        html,
        re.I | re.S,
    ):
        link = m.group(1)
        title = strip_html(m.group(2))
        if not title or "/f1/" not in link:
            continue
        found.append(NewsItem(title=title, link=link, summary="", source="formulapassion"))
        if len(found) >= max_n:
            break
    return found


def next_team_events(team_id: int, limit: int = 10) -> list[dict[str, Any]]:
    data = http_json(f"{SOFASCORE}/team/{team_id}/events/next/0")
    ev = sorted(data.get("events", []), key=lambda e: e.get("startTimestamp", 10**18))
    return ev[:limit]


def sinner_events_fallback(limit: int = 10) -> list[dict[str, Any]]:
    # Tennis doesn't reliably expose /events/next for this entity; fallback: use last and pick upcoming.
    data = http_json(f"{SOFASCORE}/team/{SINNER_TEAM_ID}/events/last/0")
    ev = data.get("events", [])
    now_ts = int(dt.datetime.now(tz=UTC).timestamp())

    def status_type(e: dict[str, Any]) -> str:
        return (e.get("status") or {}).get("type") or ""

    upcoming = [e for e in ev if e.get("startTimestamp", 0) >= now_ts and status_type(e) in {"notstarted", "inprogress"}]
    return sorted(upcoming, key=lambda e: e.get("startTimestamp", 10**18))[:limit]


def volley_next_major_events(team_id: int, limit: int = 10) -> list[dict[str, Any]]:
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


def stage_details(stage_id: int) -> dict[str, Any]:
    return (http_json(f"{SOFASCORE}/stage/{stage_id}") or {}).get("stage") or {}


def find_current_or_next_stage_id(unique_stage_id: int) -> Optional[int]:
    """Find current (inprogress) stage if any, otherwise next notstarted stage.

    Search results don't include status, so we probe a small window of candidates.
    """
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
        if not isinstance(st, int):
            continue
        # keep a small window: allow stages that started up to 7 days ago
        if st < now_ts - 7 * 86400:
            continue
        candidates.append(ent)

    candidates = sorted(candidates, key=lambda e: e.get("startTimestamp", 10**18))[:10]
    if not candidates:
        return None

    inprogress: list[int] = []
    upcoming: list[tuple[int, int]] = []  # (startTs, id)
    for ent in candidates:
        sid = int(ent["id"])
        st = int(ent.get("startTimestamp") or 0)
        try:
            det = stage_details(sid)
            status = (det.get("status") or {}).get("type") or ""
            end_ts = det.get("endDateTimestamp") or det.get("endTimestamp")
            if status == "inprogress":
                inprogress.append(sid)
                continue
            if status == "notstarted":
                upcoming.append((st, sid))
                continue
            # fallback: if we have end timestamp and we're within the window, treat as current
            if isinstance(end_ts, int) and st <= now_ts <= end_ts:
                inprogress.append(sid)
        except Exception:
            # If probe fails, ignore
            pass

    if inprogress:
        return inprogress[0]
    if upcoming:
        upcoming.sort()
        return upcoming[0][1]

    # last resort: earliest candidate
    return int(candidates[0]["id"])


def stage_substages(stage_id: int) -> list[dict[str, Any]]:
    data = http_json(f"https://www.sofascore.com/api/v1/stage/{stage_id}/substages")
    return sorted(data.get("stages", []), key=lambda s: s.get("startDateTimestamp", 10**18))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="Write payload JSON to file")
    args = ap.parse_args()

    today = dt.datetime.now(tz=ROME).date().isoformat()

    payload: dict[str, Any] = {
        "generatedAt": dt.datetime.now(tz=UTC).isoformat(),
        "tz": "Europe/Rome",
        "date": today,
        "events": {
            "fiorentina": next_team_events(FIORENTINA_ID),
            "milan": next_team_events(MILAN_ID),
            "sinner": sinner_events_fallback(),
            "volley_m": volley_next_major_events(IT_VOLLEY_M_ID),
            "volley_f": volley_next_major_events(IT_VOLLEY_F_ID),
            "f1": {
                "stageId": find_current_or_next_stage_id(F1_UNIQUE_STAGE_ID),
            },
            "motogp": {
                "stageId": find_current_or_next_stage_id(MOTOGP_UNIQUE_STAGE_ID),
            },
        },
        "news": {
            "serie_a": [asdict(x) for x in rss_items(RSS_SERIE_A, "gazzetta-serie-a")],
            "tennis": [asdict(x) for x in rss_items(RSS_TENNIS, "gazzetta-tennis")],
            "motogp": [asdict(x) for x in rss_items(RSS_MOTOGP, "gazzetta-motogp")],
            "f1": [asdict(x) for x in rss_items(RSS_F1, "gazzetta-f1")],
            "f1_extra": [asdict(x) for x in formulapassion_links()],
        },
    }

    # expand substages + include stage status
    for key in ("f1", "motogp"):
        sid = payload["events"][key]["stageId"]
        if isinstance(sid, int):
            payload["events"][key]["substages"] = stage_substages(sid)
            try:
                det = stage_details(sid)
                payload["events"][key]["status"] = (det.get("status") or {}).get("type") or ""
                payload["events"][key]["startDateTimestamp"] = det.get("startDateTimestamp")
                payload["events"][key]["endDateTimestamp"] = det.get("endDateTimestamp")
            except Exception:
                payload["events"][key]["status"] = ""

    out = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    print(out)


if __name__ == "__main__":
    main()

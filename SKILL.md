---
name: sport-briefing
description: Produce the daily sports briefing: fetch upcoming events (Sofascore) plus top news (Gazzetta RSS + FormulaPassion F1) for Fiorentina, AC Milan, Sinner, Italy volleyball, MotoGP, and Formula 1; use LLM judgment to pick 0-4 relevant news per topic and write an idempotent daily page to a Notion database.
---

# Daily briefing (Sofascore + News → Notion)

## Goal

Create/update the Notion page:

- Title: `Riepilogo Sportivo Giornaliero del YYYY-MM-DD` (Europe/Rome)
- Database: Document Hub (`315c392a8f7a80cbb1b6d16994e18f58`)
- Category: `Riepilogo Sportivo Giornaliero`
- Sections: separate tables per topic + up to 4 news items per topic.

News requirements:
- For each topic, include **0–4** news items (0 if nothing relevant).
- Each news item is **one paragraph**: a short Italian recap + the **full link**.

Time formatting:
- Human friendly (e.g. "Next Tuesday at 20:45 (Tue 02.Mar)", "In 2 weeks at 20:45 (Tue 22.Mar)").

## How to run (deterministic + intelligent)

1) Gather deterministic payload (events + RSS lists):

```bash
python3 /home/gabrielc/.openclaw/workspace/skills/daily-briefing/scripts/gather_payload.py --out /home/gabrielc/.openclaw/workspace/reports/daily-briefing/payload.json
```

2) Use LLM judgment to:
- pick the most relevant 0–4 news items per topic
- generate a **1-paragraph Italian recap** for each item (do not hallucinate; base on RSS description and, when needed, fetch the linked page with web_fetch)
- produce a `briefing.json` that matches the schema expected by `write_notion_from_briefing.py`

3) Write to Notion (idempotent):

```bash
python3 /home/gabrielc/.openclaw/workspace/skills/daily-briefing/scripts/write_notion_from_briefing.py --in /home/gabrielc/.openclaw/workspace/reports/daily-briefing/briefing.json
```

## Input/Output schema

- `payload.json` is produced by gather_payload.py
- `briefing.json` must be:

```json
{
  "pageTitle": "Riepilogo Sportivo Giornaliero del 2026-02-28",
  "intro": "...",
  "sections": [
    {
      "title": "Fiorentina",
      "table": {"header": ["Match", "When", "Competition", "Round"], "rows": [["...", "...", "...", "..."]]},
      "news": [{"title": "...", "link": "https://...", "recap": "..."}]
    }
  ]
}
```

Notion token is read from: `~/.openclaw/credentials/notion.token`.

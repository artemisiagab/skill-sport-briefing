---
name: sport-briefing
description: 'Produce the daily sports briefing: fetch upcoming events (Sofascore) plus top news (Gazzetta RSS + FormulaPassion F1) for Fiorentina, AC Milan, Sinner, Italy volleyball, MotoGP, and Formula 1; use LLM judgment to pick 0-4 relevant news per topic and write an idempotent daily page to a Notion database.'
---

# Daily Sport Briefing — Skill

Fetch events + news, select the best items, publish to Notion + markdown.

## Pipeline (3 steps)

### Step 1 — Gather data (deterministic)

```bash
python3 scripts/gather_payload.py
```

Produces two files:
- `~/.openclaw/workspace/reports/sport-briefing/events.json` — formatted event tables
- `~/.openclaw/workspace/reports/sport-briefing/news.json` — raw RSS/web candidates

### Step 2 — Select news & write recaps (LLM)

Read both files. For each section in `events.json`, look up the matching key
in `news.json → candidates` and **select 0–4** of the most relevant and fresh
news items.

| Section | Candidates key | Selection guidance |
|---|---|---|
| Fiorentina | `Fiorentina` | Only items about Fiorentina (injuries, lineup, transfers, match analysis) |
| AC Milan | `AC Milan` | Only items about AC Milan |
| Jannik Sinner | `Jannik Sinner` | Items about Sinner or directly relevant opponents |
| Italia Volley (Men) | `Italia Volley (Men)` | Items about Italian men's national volleyball team, VNL, Euros, Worlds |
| Italia Volley (Women) | `Italia Volley (Women)` | Items about Italian women's national volleyball team, VNL, Euros, Worlds |
| MotoGP (...) | `MotoGP` | Race weekend, results, riders, team news |
| Formula 1 (...) | `Formula 1` | Race weekend, results, drivers, team news |

For each selected item, write a **`recap`** field: 1–2 sentences in Italian,
factual, based on the candidate's `summary` (and if too vague, fetch the
linked page for detail). Never hallucinate facts.

Then build `briefing.json`:

```json
{
  "pageTitle": "<from events.json>",
  "intro": "<from events.json>",
  "sections": [
    {
      "title": "<from events.json>",
      "table": { "header": [...], "rows": [...] },
      "news": [
        { "title": "...", "link": "https://...", "recap": "..." }
      ]
    }
  ]
}
```

Save to `~/.openclaw/workspace/reports/sport-briefing/briefing.json`.

### Step 3 — Publish to Notion + markdown

```bash
python3 scripts/save.py
```

This:
- Creates/updates the Notion page (idempotent, by title match).
- Writes `~/.openclaw/workspace/reports/sport-briefing/daily.md`.

Notion database: Document Hub (`315c392a8f7a80cbb1b6d16994e18f58`).
Token: `~/.openclaw/credentials/notion.token`.

## Output format

### Page title
`Riepilogo Sportivo Giornaliero del YYYY-MM-DD` (Europe/Rome date)

### Sections (in order)
1. **Fiorentina** — table: Match | When | Competition | Round
2. **AC Milan** — table: Match | When | Competition | Round
3. **Jannik Sinner** — table: Match | When | Tournament | Round
4. **Italia Volley (Men)** — table: Match | When | Competition | Round
5. **Italia Volley (Women)** — table: Match | When | Competition | Round
6. **MotoGP (current/next weekend sessions)** — table: Session | Type | When
7. **Formula 1 (current/next weekend sessions)** — table: Session | Type | When

### News items
- **0–4 items** per section (0 if nothing relevant).
- Rendered under a `### Top news` sub-heading.
- Each item: `[Title](URL) — Italian recap (1–2 sentences)`.
- Recap: factual, concise, journalistic Italian. Name athletes/teams, mention scores/dates/decisions.

### "When" column — human-friendly dates
All times in **Europe/Rome**:
- Previous day → `Yesterday at HH:MM (Day DD.Mon)`
- Same day → `Today at HH:MM (Day DD.Mon)`
- Next day → `Tomorrow at HH:MM (Day DD.Mon)`
- 2–7 days → `Next Wednesday at HH:MM (Wed DD.Mon)`
- 8–13 days → `In N days at HH:MM (Day DD.Mon)`
- 14+ days → `In N weeks at HH:MM (Day DD.Mon)`

## Code conventions
- **Pure stdlib**: no pip dependencies.
- **Timezone**: always `Europe/Rome` via `zoneinfo.ZoneInfo`.
- **Idempotent writes**: find Notion page by title, create if missing, replace all children.

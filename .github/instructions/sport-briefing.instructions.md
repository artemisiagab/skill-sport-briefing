---
applyTo: '**'
description: 'Use when: working on the sport-briefing skill, generating daily briefing JSON, writing news recaps, or modifying briefing scripts'
---

# Sport Briefing — Output & Code Conventions

## Output Format (briefing.json → Notion)

The final Notion page must follow this exact structure:

### Page Title & Intro
- Title: `Riepilogo Sportivo Giornaliero del YYYY-MM-DD` (Europe/Rome date)
- Intro: `Riepilogo automatico (eventi + notizie selezionate).`

### Sections (in order)
1. **Fiorentina** — table: Match | When | Competition 
2. **AC Milan** — table: Match | When | Competition 
3. **Jannik Sinner** — table: Match | When | Tournament 
4. **Italia Volley (Men)** — table: Match | When | Competition 
5. **Italia Volley (Women)** — table: Match | When | Competition 
6. **MotoGP** — table: Session | Type | When
   - Title: `MotoGP (current weekend sessions)` when a race weekend is in progress, otherwise `MotoGP (next weekend sessions)`
7. **Formula 1** — table: Session | Type | When
   - Title: `Formula 1 (current weekend sessions)` when a race weekend is in progress, otherwise `Formula 1 (next weekend sessions)`

### "When" Column — Human-Friendly Dates
All times in **Europe/Rome** timezone, English weekday/month abbreviations:
- before yesterday → `Last Sunday at HH:MM`
- Previous day → `Yesterday Sun at HH:MM `
- Same day → `Today at HH:MM (Mon DD.Mon)`
- Next day → `Tomorrow at HH:MM (Tue DD.Mon)`
- 2–7 days → `Next Wednesday at HH:MM (Wed DD.Mon)`
- 8–13 days → `In N days at HH:MM (Thu DD.Mon)`
- 14+ days → `In N weeks at HH:MM (Fri DD.Mon)`

### News Items
- **0–4 items per section** (0 if nothing relevant).
- Rendered under a `### Top news` sub-heading.
- Each item is **one paragraph**: `[Title](URL) — Italian recap`.
- The recap is **1–2 sentences in Italian**, factual, based on the source content — never hallucinated.
- The recap should summarize the key takeaway, name relevant athletes/teams, and mention concrete facts (scores, injuries, dates, decisions).
- Do **not** copy the original text verbatim; rephrase into a concise journalistic style.
- News links come from Gazzetta dello Sport RSS feeds and FormulaPassion (F1 only).

## LLM Step (Step 2 of the Workflow)

When processing `payload.json` → `briefing.json`:

1. Read the `newsCandidates` list for each section.
2. **Select 0–4** most relevant and recent items per topic. Prefer:
   - Team/player news (injuries, lineup, transfers) over generic league coverage.
   - Pre-match or post-match analysis over tangential stories.
   - Breaking news over recurring features.
3. For each selected item, write the `recap` field: a 1–2 sentence Italian summary.
4. If the RSS description is too vague to write a solid recap, fetch the linked page for more detail.
5. Remove the `newsCandidates` key from each section before saving `briefing.json`.
6. Keep `table` data as-is (deterministic, already formatted).

## Code Conventions

- **Pure stdlib**: no pip dependencies — use only `urllib`, `json`, `xml.etree`, `dataclasses`, `zoneinfo`, etc.
- **Timezone**: always `Europe/Rome` via `zoneinfo.ZoneInfo`.
- **Notion token**: read from `~/.openclaw/credentials/notion.token`.
- **Output directory**: `~/.openclaw/workspace/reports/daily-briefing/`.
- **Idempotent writes**: find page by title, create if missing, replace all children.

# Herald Growth — Do's

*What we must ALWAYS do when working on Herald Growth. Check BEFORE every action.*

---

## Browser (Playwright)

1. Always save browser state after session — `data/browser_state.json`
2. Save debug screenshots on every failure — `data/screenshots/`
3. Check logged-in state before write operations — auto re-login if expired
4. Human delay between actions (3-5s) — never machine-gun clicks
5. Session cookies persist between runs — no login needed every time

## Reactions

1. Reactions via Playwright browser — dev.to API doesn't support reactions for regular users
2. Weighted reaction types: like (common), unicorn/fire/etc (occasional) for authenticity
3. Filter own articles, filter already-reacted
4. Max 10 reactions per run, hourly cron

## Comments

1. Comments via `nathan-team herald_growth --run` — needs Claude for content generation
2. Quality gate on comments: genuine, specific to the article, not generic
3. dev.to API POST /comments is admin-only — must use Playwright browser
4. 5 comments per cycle, 3 cycles per day

## Cron

1. Reactions: `python -m growth.reactor` — direct Python, runs in venv
2. Comments: `nathan-team herald_growth --run` — Claude generates, Playwright posts
3. Team must be registered in `~/.claude/teams.json` — cron fails with "Unknown team" if not

## Data

1. All state in `data/` — reacted.json, commented.json, engagement_log.jsonl
2. Browser state in `data/browser_state.json` — do NOT commit this file
3. Screenshots in `data/screenshots/` — debug artifacts, do NOT commit

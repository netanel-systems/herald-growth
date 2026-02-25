# Herald Growth — Dev.to Engagement Engine

> React fast. Comment genuinely. Track reciprocity. Grow followers.
> This goes under Klement's name. Guard his reputation above all.

---

## Architecture

Two-speed design with **Playwright browser automation** for write operations:

| Speed | Engine | Frequency | Cost | Method |
|-------|--------|-----------|------|--------|
| **Reactions** | Python cron (`growth.reactor`) | Every 10 min | $0 (no LLM) | Playwright headless browser |
| **Comments** | nathan-team (`herald_growth`) | 3x daily | ~$0.10-0.30/run | Nathan writes, Playwright posts |

**Setup required:** Browser credentials in `.env` file. See [Configuration](#configuration) section below.

### Why Browser Automation?

The dev.to Forem API does NOT support reactions or comments for regular users:
- `POST /reactions` — admin-only (`require_admin` in controller source)
- `POST /comments` — route does not exist in API namespace

Confirmed from Forem source code. API client is used for **reads only** (article discovery, comment reading, followers). Playwright handles all **writes** (reactions, comments).

### Module Responsibilities

```text
growth/
├── browser.py    ← Playwright headless Chromium (login, react, comment, reply)
├── scout.py      → API GET (article discovery)         — no change
├── reactor.py    → browser.react_to_article()           — no change
├── commenter.py  → browser.post_comment()               — no change
├── responder.py  ← NEW: engage with comments on OUR own articles
├── learner.py    → local file analytics                 — no change
├── tracker.py    → API GET (followers, reciprocity)     — no change
├── client.py     → API GET only (reads)                 — +get_articles_by_username, +get_article_comments
├── config.py     → +6 browser settings                  — no new changes
└── storage.py    → shared JSON utilities                — no change
```

## Own-Post Engagement Rules (NON-NEGOTIABLE)

These rules govern how we interact with comments on our own articles. Violations
damage Klement's reputation and will be logged.

1. **One comment per post on others' content — no thread continuation.** We reply
   once to someone else's post, then stop. Never continue a thread we started.
2. **Every comment received on our own articles = like + reply.** No silent reads.
   Every comment deserves both a like (show appreciation) and a specific reply.
3. **Like the post before replying to it.** When commenting on someone else's content,
   react first, then reply. This applies in commenter.py cycles.
4. **Max engagement depth: 1 reply per incoming comment.** We respond to the first
   comment from any person. We do not reply to our own reply. Thread ends there.
5. **Dedup is strict.** responded_comments.json is the source of truth. Any comment
   ID already in that file is skipped without exception.
6. **Reply must be specific.** The reply must reference something the commenter said.
   Generic replies ("Thanks for reading!") are a CRITICAL violation.

### Responder Cron (2x daily)

Runs AFTER the main engagement cycles.

```cron
# Own-post comment engagement — 9 AM UTC
0 9 * * * cd ~/netanel/teams/herald_growth && .venv/bin/python -m growth.responder_main

# Own-post comment engagement — 3 PM UTC
0 15 * * * cd ~/netanel/teams/herald_growth && .venv/bin/python -m growth.responder_main
```

### Browser Session Flow

1. On first run: login via `dev.to/enter` form, save cookies to `data/browser_state.json`
2. On subsequent runs: load saved cookies, verify logged in, proceed
3. If session expired: auto re-login with stored credentials
4. Debug screenshots saved to `data/screenshots/` on any failure

### Error Handling

| Error | Exception | Behavior |
|-------|-----------|----------|
| Session expired, no credentials in `.env` | `BrowserLoginRequired` | Cycle aborts, logged as error |
| Playwright crash, browser launch failure | `BrowserError` | Cycle aborts, next cron retry |
| Navigation timeout (dev.to slow/down) | Caught internally | Article skipped, screenshot saved, cycle continues |
| Element not found (selector changed) | Caught internally | Screenshot saved, article skipped |
| CAPTCHA or challenge page detected | `BrowserLoginRequired` | Cycle aborts, screenshot saved for manual review |
| Already reacted to article | None | Returns `(True, False)`, counted as success |

**Selectors** (from Forem source code — verified, not guessed):
- Login form: `input[autocomplete="email"]`, `input[autocomplete="current-password"]`
- Login submit: `[data-testid="login-form"] button[type="submit"]`
- Logged-in check: `meta[name="user-signed-in"][content="true"]`
- Reaction buttons: `#reaction-butt-{category}` (like, unicorn, fire, raised_hands, exploding_head)
- Activated state: `.user-activated` class on button
- Drawer trigger: `#reaction-drawer-trigger` (hover to open for non-like reactions)
- Comment form: `form.comment-form`, textarea: `textarea.comment-textarea`
- Comment submit: `.comment-form button[type="submit"]`

**Human-like behavior:** Random delays (0.5-5s) between all actions. No instant clicks.

### Testing / Verification

- **Login smoke test:** Start browser with `GROWTH_BROWSER_HEADLESS=false`, verify `dev.to/enter` login, check signed-in meta tag.
- **Reaction smoke test:** Attempt single reaction on a test article, verify `.user-activated` class appears.
- **Comment smoke test:** Post a short test comment to a safe article, verify it appears on the page.
- **CAPTCHA handling:** Verify detection path aborts cycle with screenshot saved to `data/screenshots/`.
- **Selector resilience:** Confirm key selectors (`#reaction-butt-like`, `form.comment-form`) still resolve on current dev.to.
- **Session persistence:** Login once, stop browser, restart, verify no re-login needed.
- **Run locally:** `GROWTH_BROWSER_HEADLESS=false python -m growth.reactor` — watch reactions happen visually.

### Configuration

Browser automation settings in `growth/config.py` (loaded from `.env` with `GROWTH_` prefix):

| Env Variable | Default | Purpose |
|---|---|---|
| `GROWTH_DEVTO_EMAIL` | (required) | dev.to login email |
| `GROWTH_DEVTO_PASSWORD` | (required) | dev.to login password |
| `GROWTH_BROWSER_HEADLESS` | `true` | Headless mode (true for cron, false for debug) |
| `GROWTH_BROWSER_TIMEOUT` | `30` | Page timeout in seconds |
| `GROWTH_BROWSER_USER_AGENT` | Chrome UA | Browser user agent string |
| `GROWTH_USE_BROWSER` | `true` | Enable browser for writes (false = API-only fallback) |

Add to `.env`:

```env
GROWTH_DEVTO_EMAIL=your-email@example.com
GROWTH_DEVTO_PASSWORD=your-password
```

## Context

Nathan: load these before every comment cycle:
- `~/.nathan/teams/herald_growth/knowledge/comment-style-guide.md`
- `~/.nathan/teams/herald_growth/DOS.md`
- `~/.nathan/teams/herald_growth/MEMORY.md`

---

## Reaction Cycle (Python, autonomous)

Runs via cron every 10 minutes. No LLM needed. Uses Playwright browser.

**Browser rate limits:** Max 10 reactions per run, 3-5s between each reaction click. One browser instance at a time. See [Rate Limits](#rate-limits) for API limits.

1. Load `data/reacted.json` (dedup set)
2. Scout rising + fresh articles across 15 target tags
3. Filter: already reacted, own articles
4. React to top N with varied categories (weighted: like 50%, fire 25%, raised_hands 15%, exploding_head 10%)
5. Log each reaction to `data/engagement_log.jsonl`
6. Save updated `data/reacted.json`

**Entry point:** `python -m growth.reactor`

---

## Comment Cycle (Nathan, 3x daily)

Nathan reads articles, writes comments. The human touch.

1. Load `data/commented.json` + `data/reacted.json` (dedup)
2. Load learnings from `data/learnings.json` (apply what works)
3. Scout top 5 commentable articles (rising + hot, min 3 reactions, not yet engaged)
4. For each article:
   a. Fetch full content (`growth.scout.get_article_content`)
   b. READ the article (understand what it's about)
   c. Write a 1-2 sentence comment that references something SPECIFIC
   d. Post via `growth.commenter.post_comment` (quality gate validates before posting)
5. React to 10 more articles (mix of rising + fresh)
6. Log everything to engagement_log.jsonl
7. Save updated commented.json

**Entry point:** `nathan-team herald_growth --run "Comment cycle: ..."`

---

## Comment Quality Rules (NON-NEGOTIABLE)

### DO
- 1-2 sentences MAX
- Reference ONE specific thing from the article
- Add tiny value: related experience, gotcha, or question
- Vary the style (rotate patterns from comment-style-guide.md)
- Sound like a real developer typing quickly

### DON'T
- Paragraphs (instant violation)
- "Great article!" or any generic praise (violation)
- Mention our company, articles, or products (violation)
- "As someone who..." introductions (AI tell)
- Always end with a question (pattern detection)

---

## Data Files

| File | Purpose | Bounded |
|------|---------|---------|
| `data/reacted.json` | Article IDs we reacted to | 2,000 max |
| `data/commented.json` | Article IDs we commented on | 1,000 max |
| `data/engagement_log.jsonl` | Full audit trail of all actions | 10,000 max |
| `data/comment_history.jsonl` | Detailed comment log for learner | Unbounded (trim manually) |
| `data/learnings.json` | Accumulated insights | 200 max |
| `data/follower_snapshots.jsonl` | Follower count over time | Append-only |
| `data/weekly_report.json` | Latest weekly analysis | Overwritten weekly |
| `data/target_tags.json` | Tags we monitor | 15 tags |
| `data/browser_state.json` | Playwright session cookies | Overwritten per session |
| `data/screenshots/` | Debug screenshots on failures | Auto-cleaned |

---

## Target Tags (15)

ai, python, machinelearning, langchain, programming, beginners, tutorial,
webdev, javascript, devops, productivity, architecture, opensource, career, discuss

---

## Rate Limits

### API (reads)
- Forem API: 30 requests per 30-second rolling window
- Never exceed 20 API requests in any single cycle

### Browser (writes)
- Reactions: max 10 per 30s (Forem app limit), our rate: 10 per 10-min cycle (3-5s between clicks)
- Comments: max 9 per 30s (Forem app limit), our rate: 5 per cycle (30-60s between posts)
- One headless browser instance at a time — no parallel sessions
- Human-like delays: 0.5-5s random between all browser actions

---

## Brain Files

| File | Purpose |
|------|---------|
| `~/.nathan/teams/herald_growth/MEMORY.md` | What we know |
| `~/.nathan/teams/herald_growth/DOS.md` | Pre-flight checklist |
| `~/.nathan/teams/herald_growth/VIOLATIONS.md` | Accountability |
| `~/.nathan/teams/herald_growth/REWARDS.md` | Points tracking |
| `~/.nathan/teams/herald_growth/UNKNOWNS.md` | Open questions |
| `~/.nathan/teams/herald_growth/state.json` | Session state |
| `~/.nathan/teams/herald_growth/knowledge/comment-style-guide.md` | Comment rules |

---

## Bug Fixes Applied (2026-02-23)

Seven bugs fixed in the `fix/growth-bugs` branch. System state after fixes:

| # | File | Bug | Fix |
|---|------|-----|-----|
| A | `reactor.py` | Rate limit `break` stopped entire cycle | Changed to `continue`; logs how many articles remain |
| B | `learner.py` | `get_reaction_count()` returned 0 (read missing `count` field) | Now reads `len(article_ids)` — the actual list length |
| C | `browser.py` | CAPTCHA `text=` selector invalid in Playwright `locator()` | Moved to `CAPTCHA_TEXT_INDICATORS`; uses `get_by_text()` in detection loop |
| D | `browser.py` | Drawer hover delay too short (0.5-1.0s) for non-like reactions | Replaced fixed sleep with `wait_for(state="visible", timeout=3000)` + fallback |
| E | `commenter.py` | Quality gate used substring match — false positives on "totally agree, here's why..." | Regex word boundaries (`\b`); sentence split uses lookbehind `(?<=[.!?])\s+` |
| F | `commenter.py` | `trim_engagement_log()` called per-comment inside `_log_engagement()` — O(N²) | Removed from `_log_engagement()`; callers must call trim once after full cycle |
| G | `storage.py` | No docstring explaining `article_ids` vs `count` field semantics | Added clear docstrings to `load_json_ids()` and `save_json_ids()` |
| H | `storage.py` | Temp file cleanup on `atomic_write_json()` failure was silent | `OSError` during cleanup now logged as warning instead of silently swallowed |

### Key Invariant (storage.py)

`reacted.json` and `commented.json` format:

```json
{"article_ids": [1, 2, 3], "count": 3}
```

`count` is informational. Always use `len(article_ids)` for the actual count.

---

## Monitoring Integration

The growth dashboard (`teams/monitoring`) reads this team's data to report **dev.to platform metrics**.
Herald Growth is the dev.to engagement engine — the monitoring collector reads three files from this team:

| File | What it tracks | Read by |
|------|---------------|---------|
| `teams/herald_growth/data/follower_snapshots.jsonl` | dev.to follower count (written by tracker.py) | `collect_devto()` in monitoring |
| `.nathan/teams/herald_growth/state.json` | `total_reactions`, `total_comments` (lifetime totals) | `collect_devto()` in monitoring |
| `teams/herald_growth/data/engagement_log.jsonl` | Per-action engagement log | Future analytics |

**Rule:** Do NOT create a separate "Herald Growth" dashboard card. This team's data feeds the unified dev.to card. If you add new metrics to state.json, update `collect_devto()` in `teams/monitoring/monitoring/collector.py` to surface them.

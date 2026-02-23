# Herald Growth — Dev.to Engagement Engine

> React fast. Comment genuinely. Track reciprocity. Grow followers.
> This goes under Klement's name. Guard his reputation above all.

---

## Architecture

Two-speed design:

| Speed | Engine | Frequency | Cost |
|-------|--------|-----------|------|
| **Reactions** | Python cron (`growth.reactor`) | Every 30 min | $0 (no LLM) |
| **Comments** | nathan-team (`herald_growth`) | 3x daily | ~$0.10-0.30/run |

## Context

Nathan: load these before every comment cycle:
- `~/.nathan/teams/herald_growth/knowledge/comment-style-guide.md`
- `~/.nathan/teams/herald_growth/DOS.md`
- `~/.nathan/teams/herald_growth/MEMORY.md`

---

## Reaction Cycle (Python, autonomous)

Runs via cron every 30 minutes. No LLM needed.

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

---

## Target Tags (15)

ai, python, machinelearning, langchain, programming, beginners, tutorial,
webdev, javascript, devops, productivity, architecture, opensource, career, discuss

---

## Rate Limits

- Forem API: 30 requests per 30-second rolling window
- Reaction delay: 2.0s between reactions
- Comment delay: 3.0s between comments
- Never exceed 20 requests in any single cycle

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

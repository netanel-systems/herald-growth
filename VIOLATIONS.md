# Herald Growth — Violations

*What went wrong. Every mistake logged, every lesson learned.*

---

| Date | ID | What Happened | Rule Broken | Impact | Correction |
|------|----|---------------|-------------|--------|------------|
| 2026-02-23 | HG-V001 | Comment crons failed with "Unknown team" error | Verify team registration before deploying crons | 3 comment cycles failed (7:30 AM, 12 PM, 5:30 PM) — zero comments posted all day | Team was registered in teams.json but cron ran before registration was saved. Fixed now, verified with `nathan-team list`. |
| 2026-02-23 | HG-V002 | dev.to Forem API doesn't support reactions/comments for regular users — discovered after building API-based reactor | Research API capabilities BEFORE building — verify with source code, not just docs | Entire API reactor had to be replaced with Playwright browser automation | Built Playwright browser module, API client stays for reads only |
| 2026-02-23 | HG-V003 | Browser login detection only checked `meta[name="user-signed-in"]` — fails on some pages | Use multiple detection methods with fallbacks | Reactor reported 0 reactions while browser was actually logged in | Added 5 fallback selectors (avatar, Create Post, notifications, nav menu) |
| 2026-02-23 | HG-V004 | No credential validation at browser startup — browser launches then fails on login | Validate preconditions before starting expensive resources | Wasted time/resources launching Chromium with no way to authenticate | Added credential check in `start()` — raises `BrowserLoginRequired` before launching browser |
| 2026-02-23 | HG-V005 | Non-atomic JSON writes in storage.py, tracker.py, learner.py | All file writes must be atomic (temp + rename) | Data loss risk on crash during write | Added `atomic_write_json()` using tempfile + `os.replace()` |

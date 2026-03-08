# Herald Growth — Unknowns

*What we don't know yet. Check here BEFORE saying "I don't know."*

---

## Resolved

| Date | Question | Answer |
|------|----------|--------|
| 2026-02-23 | Can dev.to API post reactions? | No. POST /reactions requires admin role (`require_admin` in Forem controller). Regular users must use browser. |
| 2026-02-23 | Can dev.to API post comments? | No. POST /comments route doesn't exist in the API namespace for regular users. Must use browser. |
| 2026-02-23 | How does browser session persistence work? | Playwright `context.storage_state()` saves cookies/localStorage to JSON. Load on next run to skip login. |

## Open

| Date | Question | Context |
|------|----------|---------|
| 2026-02-23 | Does dev.to detect automated browsing? | We use human delays and realistic user-agent, but unclear if they fingerprint headless browsers. |
| 2026-02-23 | What happens if browser session expires mid-cycle? | Auto re-login should handle it, but hasn't been tested in production. |

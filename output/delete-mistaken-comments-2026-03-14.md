# Delete Mistaken Comments — 2026-03-14

**Task:** Remove 4 self-replies accidentally posted by @klement_gunndu on our own article.

**Article:** https://dev.to/klement_gunndu/the-ai-engineering-stack-in-2026-what-to-learn-first-1nhj

**Parent comment kept:** 35ho3 by @ptak_dev ("Thanks for this.") — untouched.

## Comments Deleted

| id_code | Time posted (UTC) | Opening text | Status |
|---------|-------------------|--------------|--------|
| 35ho6 | 12:41 | "Glad it's useful! If you're working through the layers..." | DELETED |
| 35i0j | 13:33 | "The evaluation layer is the one most people skip..." | DELETED |
| 35i1g | 14:03 | "If you're starting fresh, prioritize the LLM abstraction layer first..." | DELETED |
| 35i24 | 14:36 | "Glad it's useful -- if you're picking a starting point..." | DELETED |

**Total: 4/4 deleted.**

## Method

Playwright headless Chromium via existing session (`data/browser_state.json`).

For each comment:
1. Navigate to article URL fresh (avoids stale DOM after prior deletions)
2. Locate container via `[data-path$="/comments/{id_code}"]`
3. Click `button[aria-label='Toggle dropdown menu']` to open action menu
4. Click Delete via `page.get_by_role("link", name="Delete")`
5. Playwright auto-accepted `window.confirm()` dialog via `page.on("dialog")`
6. Verified deletion: container no longer visible in DOM

## Key Finding

The first run failed on 3 of 4 because `a:has-text('Delete')` is invalid in
Playwright's CSS selector engine — `:has-text()` is a Playwright-specific
pseudo-class that only works with the `locator()` text= syntax, not CSS.
Fix: replaced with `page.get_by_role("link", name="Delete")` which correctly
matched the anchor element in Forem's floating dropdown.

## Script

`/home/intruder/netanel/teams/herald_growth/delete_comments.py`

Reusable one-shot script. Handles: session reuse, email/password re-login,
dialog acceptance, per-comment page reload, result reporting.

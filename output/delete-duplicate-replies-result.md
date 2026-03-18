# Delete Duplicate Replies — Result

**Date:** 2026-03-18
**Article:** Build a RAG Pipeline in Python That Actually Works
**Article ID:** 3321258
**Article URL:** https://dev.to/klement_gunndu/build-a-rag-pipeline-in-python-that-actually-works-28dg

---

## Summary

Both duplicate replies were identified and successfully deleted.

| User | Deleted id_code | Posted at | Confirmed |
|------|----------------|-----------|-----------|
| nyrok | 35ab1 | 2026-03-08T06:34:05Z | Yes — "Comment deleted" placeholder visible |
| soytuber | 35aph | 2026-03-08T14:33:45Z | Yes — "Comment deleted" placeholder visible |

---

## Root Cause

`MAX_REPLIES_PER_COMMENTER` was previously set to 3 in `growth/responder.py`. The own-post
responder ran multiple cycles on article 3321258, and because the per-commenter limit was 3
(not 1), it sent a second reply to each user:

- **nyrok** posted a second top-level comment (`35a9f`) on 2026-03-08. The responder replied
  with `35ab1`, making it the second klement_gunndu reply to nyrok on this article.

- **soytuber** replied to klement's first reply (`35anl`) with comment `35anp`. The responder
  treated that as a new incoming comment and replied again with `35aph`, making it the second
  klement_gunndu reply to soytuber on this article.

`MAX_REPLIES_PER_COMMENTER` has since been corrected to 1.

---

## Identification Method

1. Fetched all comments on article 3321258 via `GET /api/comments?a_id=3321258` (API key auth).
2. Walked the full comment tree recursively.
3. Found klement_gunndu comments that constitute the second engagement per user:
   - `replied_per_article.json` confirmed `nyrok: 2` and `soytuber: 2` on article 3321258.
   - First reply to nyrok: `358km` at 2026-03-07T12:02:46Z (direct reply to nyrok's comment `358jb`) — KEPT.
   - Second reply to nyrok: `35ab1` at 2026-03-08T06:34:05Z (reply to nyrok's second comment `35a9f`) — DELETED.
   - First reply to soytuber: `35anl` at 2026-03-08T14:02:56Z (direct reply to soytuber's comment `35an3`) — KEPT.
   - Second reply to soytuber: `35aph` at 2026-03-08T14:33:45Z (reply to soytuber's follow-up `35anp`) — DELETED.

---

## Deletion Process

Used `DevToBrowser.delete_comment()` workflow via Playwright headless Chromium:

1. Navigate to article URL.
2. Scroll page to load all comments.
3. Locate comment container via `[data-path$="/comments/{id_code}"]`.
4. Click "..." menu button (`button[aria-label='Toggle dropdown menu']`).
5. Click "Delete" link in the dropdown (`get_by_role("link", name="Delete")`).
6. Click confirmation "Delete" button in the page-level confirm modal (`get_by_role("button", name="Delete")`).
7. Verify: re-navigate to article, confirm container shows "Comment deleted" placeholder.

Note: Dev.to uses a page-level confirmation modal (not `window.confirm()`). The existing
`delete_comment()` in `browser.py` handles `window.confirm()` dialogs only. The page-level
modal required clicking a second "Delete" button (role=button) after the dropdown "Delete" link
(role=link). This distinction is documented for future `delete_comment()` improvements.

---

## Post-Deletion State

**nyrok thread on article 3321258:**
- klement_gunndu has 1 reply: `358km` (2026-03-07T12:02:46Z) — reply to nyrok's first comment.
- `35ab1` is now a "Comment deleted" placeholder.

**soytuber thread on article 3321258:**
- klement_gunndu has 1 reply: `35anl` (2026-03-08T14:02:56Z) — reply to soytuber's comment.
- `35aph` is now a "Comment deleted" placeholder.

**replied_per_article.json updated:**
- `3321258.nyrok`: 2 -> 1
- `3321258.soytuber`: 2 -> 1

---

## Files Changed

- `data/replied_per_article.json` — corrected reply counts for nyrok and soytuber on article 3321258.

## Script Used

`delete_duplicates.py` (one-off, in `~/netanel/teams/herald_growth/`)

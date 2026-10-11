# 0001 — Ed Discussion API facts and the sync model

Status: accepted, 2026-10-11. Source: observed traffic from a logged-in edstem.org/au session, confirmed by the live verification of the first release.

## Context

EdScrape reads Ed Discussion for a student's units and surfaces teaching-team messages. Ed has no public API documentation; everything below was observed, and several facts contradict obvious assumptions. Changing `api.py`, `sync.py`, `flags.py` or `edparse.py` without knowing them will break the tool quietly.

## Decisions

**GET-only client.** The web app mutates read state with `POST /api/threads/{id}/read` and `/glance`, refreshes tokens with `POST /api/renew_token`, and counts views with `?view=1` on thread detail. The client sends none of these, so a sync is invisible to Ed and never marks anything read on the server. `EdClient` exposes only GET methods and a test asserts that.

**Auth is `x-token`, read from a browser profile.** API calls carry an `x-token: <JWT>` header (`Authorization: Bearer` is rejected). The SPA stores the JWT in `localStorage.authToken`; it lives 14 days. Login launches a persistent Playwright Chromium profile, waits for that key, and stores the token at `~/.local/share/edscrape/token` (0600). On 401 the tool asks for a new login; it never calls `renew_token`.

**Sync pages the whole list and diffs.** `GET /api/courses/{id}/threads` has no "updated since" filter and sorts only by created/votes/views, so incremental sync lists every thread (`limit=100`, 2–3 requests per unit, deduped by id because offsets shift) and compares a change key per thread (`updated_at`, `reply_count`, `editor_id`, `is_endorsed`, `accepted_id`, `is_answered`, `is_pinned`, hash of the body). Thread bodies come from the list; `GET /api/threads/{id}` is fetched only for threads with replies. Because it is unproven that `updated_at` moves when a nested comment is edited, `full_recheck = true` re-fetches every replied thread on each sync (about 230 requests for a 290-thread unit, ~30 s). `recheck_days` applies only when that is off.

**`content` is the markup, `document` is the plain text.** The XML-like `<document version="2.0">` markup is in `content`; `document` is its text rendering. `edparse` parses `content` to Markdown, uses `document` as the FTS text and as the fallback when the XML is malformed, and reports unknown tags rather than dropping them.

**Edits are signalled only by `editor_id`.** There is no `edited_at`/`is_edited`, and `updated_at` also moves on non-edit activity. A post counts as edited when `editor_id` is set or its body hash changed between syncs; every changed body is kept in `post_versions` because Ed exposes no edit history.

**Post keys, not ids.** Thread ids and answer/comment ids are separate sequences that overlap across years, so every stored post is keyed `thread:<id>`, `answer:<id>` or `comment:<id>`.

**Roles come from `course_role` on each post.** The members endpoint is empty for courses where the user is a student. `admin`, `staff`, `tutor` map to staff; `mentor` to helper; a known non-null role is never overwritten by the `null` Ed returns after someone leaves a course. Anonymous posts carry no role and therefore cannot be identified as teaching-team messages.

**Anonymity is enforced locally.** In a course where the user is staff, Ed's payload reveals the real author of anonymous posts (including via `editor_id`). The tool computes `is_own` first, then stores anonymous posts with no user id, the name "Anonymous" and `editor_id = -1` when an editor existed; such authors are never written to `users`.

**Deleted comments are tombstones.** A deleted item keeps its slot with `type: ""`, zero dates and `user_id: 0`; its replies remain. Rows are kept with `is_deleted = 1` so children still render. Absence from the list is never treated as deletion.

**Briefs run on the user's Claude subscription.** `brief` pipes staff/helper messages (student context only as author-less excerpts ≤300 chars, titles capped at 120) into `claude -p --tools "" --no-session-persistence --system-prompt-file …`, cached by a fingerprint of the input so an unchanged assignment makes no call. The system-prompt file holds those excerpts until the next build and is 0600.

## Consequences

- Any new endpoint must be observed first; see `docs/scratchpad/edscrape-v1/recon.md` while it exists (local only).
- Lowering sync cost means turning off `full_recheck`, accepting a bounded miss window for edits to old comments.
- Read state is entirely local and manual.

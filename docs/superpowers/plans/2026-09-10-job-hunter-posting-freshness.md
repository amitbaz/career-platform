# Plan: posting freshness (#186)

Design: `docs/superpowers/specs/2026-09-10-job-hunter-posting-freshness-design.md`.

Test-first, one vertical slice at a time, at the two agreed seams (the stage
against the local stack; pgTAP). Migration placeholder:
`29999999000000_job_hunter_posting_freshness.sql` — numeric, so
`supabase db reset` applies it; the real timestamp is requested at PR-open.
Verify SQL only after `pnpm db:reset`.

1. **pgTAP — schedule.** Columns; `job_hunter_freshness_interval` widens with
   age and is clamped; a new posting is not due on discovery;
   `job_hunter_enqueue_due_freshness` sends only due, open, checkable,
   surviving postings, never twice, and leases them; neither is reachable by a
   user; the cron entry exists.
2. **Stage — a removed page closes the posting**, and the posting is no longer
   pending delivery but still readable.
3. **Stage — an unchanged posting** costs one conditional request, bumps the
   check and pushes the next one out further for an older posting; nothing is
   enqueued.
4. **Stage — ATS channel.** Absent from the board closes; a changed
   `official_ats` description updates the posting and enqueues
   `extract_facets`; one board fetch serves every posting on it.
5. **Stage — what is not evidence.** Timeout is transient, 429 is quota, 403
   is `unverified` and stays open.
6. **pgTAP — readers and reopening.** Pending delivery skips a closed posting;
   `job_hunter_merge_posting_batch` reopens one. Python: `crawl_source` lets a
   closed posting through its short-circuit; `extract_facets` skips a closed
   posting.
7. **Consumer.** `recheck-freshness` CLI drain with a logged outcome summary
   (an empty drain says why), plus `.github/workflows/job-hunter-recheck-freshness.yml`.
8. **Docs.** AGENTS.md (root migration list, Job Hunter stage list), CONTEXT.md
   if a term needs it.
9. `/code-review`, full Job Hunter suite, full pgTAP suite from a clean reset,
   commit.

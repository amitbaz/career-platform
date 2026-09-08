# Merged-Job Cover-Letter Implementation Plan

**Goal:** Implement issue #146 at the shared on-demand cover-letter seam.

**Architecture:** Resolve a missing requested job id through the existing
Postgres merge redirect, then consistently use the surviving id for all
cover-letter reads and writes. Notify the user only when no live job or redirect
exists.

**Spec:** `apps/job-hunter/docs/superpowers/specs/2026-09-08-merged-job-cover-letter-design.md`

## Task 1: Lock the behavior with regression tests

- [x] Add a test that merges a delivered card's job id and generates a new
  cover letter for the survivor.
- [x] Add a test that resends existing survivor material without Gemini.
- [x] Add a test that a genuinely missing job gets an informative Telegram
  message and makes no model call.
- [x] Run the three tests and confirm they fail for the missing behavior.

## Task 2: Follow the merge redirect

- [x] Resolve an absent requested id with `resolve_merged_job_id`.
- [x] Use the surviving id for evaluation and material reads, material writes,
  and document delivery records.
- [x] Send a generic no-longer-available message when no redirect exists.
- [x] Run the focused regression tests and all on-demand cover-letter tests.

## Task 3: Verify and review

- [x] Run the complete Job Hunter test suite with the repository's local
  Supabase test environment.
- [ ] Review the branch against repository standards and issue #146.
- [ ] Address review findings, rerun affected tests, and commit the result.

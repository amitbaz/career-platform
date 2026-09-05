# LinkedIn "Sign in" Legacy Cleanup Design

## Problem

Production verification after PR #7 showed that the historical `gmail:linkedin` artifacts were not reprocessed. The guarded cleanup only considered materialized LinkedIn jobs safe to release when both `company` and `title` were blank. Historical scraping had populated 831 poisoned jobs with `title = "Sign in"` after following LinkedIn URLs to the login page, so those rows were incorrectly treated as legitimate populated jobs and blocked cleanup.

The affected staged candidates are still blank and the affected jobs have no evaluations, materials, deliveries, or application-event dependencies. They are legacy artifacts, not useful job data.

## Goal

Allow the existing guarded LinkedIn cleanup to recognize the known login-page title `"Sign in"` as poisoned legacy metadata, so safe historical LinkedIn Gmail messages are released and reprocessed through the new enrichment/deduplication path.

## Scope

Change only `src/job_hunter/gmail_linkedin_cleanup.py` and focused tests in `tests/test_linkedin_gmail_enrichment.py`.

No changes to normal job-title filtering, semantic classification, LinkedIn enrichment, source identity, discovery, ranking, or Telegram rendering.

## Design

Add a private predicate in the cleanup module that decides whether a materialized `gmail:linkedin` job is legacy-poisoned:

- `company` must be blank after trimming.
- `title` may be blank, preserving current behavior.
- `title` may also equal `"sign in"` after trimming and case-folding.
- Any other non-empty title is treated as real data and blocks cleanup.
- Any non-empty company blocks cleanup, including a hypothetical legitimate job whose title is `"Sign in"`.

Use this predicate only inside `release_legacy_blank_linkedin_jobs()` when checking matched materialized jobs.

The existing dependency guard remains unchanged: a job is never deleted if it has an evaluation, material, delivery, or application-event dependency.

## Safety Invariants

1. Cleanup remains restricted to Gmail messages classified as `JOB_ALERT` with blank LinkedIn inbound candidates.
2. A materialized job with any non-empty company is preserved.
3. A materialized job with a title other than blank/`"Sign in"` is preserved.
4. A `"Sign in"` job with any dependent record is preserved.
5. Dry-run behavior remains read-only.
6. The normal application never globally treats `"Sign in"` as an invalid job title; this is a legacy-cleanup-only exception.

## Testing

Add regression tests proving:

- a blank-candidate / blank-company / `"Sign in"` materialized LinkedIn artifact is released;
- comparison is tolerant of whitespace/case from the historical scraper;
- a `"Sign in"` artifact with an evaluation dependency is retained;
- a materialized job with a non-empty company is retained even if its title is `"Sign in"`;
- the existing full suite remains green.

## Success Criteria

On the next writable Gmail sync, the production legacy rows whose only populated title is the LinkedIn login-page value `"Sign in"` become eligible for the same safe release/reprocessing mechanism already implemented for fully blank rows. Subsequent production verification should show fewer poisoned `gmail:linkedin` rows and enriched LinkedIn candidates/jobs beginning to enter the normal pipeline.
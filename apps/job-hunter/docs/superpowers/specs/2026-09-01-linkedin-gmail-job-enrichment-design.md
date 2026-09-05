# LinkedIn Gmail Job Enrichment Fix Design

## Problem

LinkedIn job-alert emails are detected deterministically from `jobalerts-noreply@linkedin.com` and `/jobs/view/...` links. Because deterministic detection already creates `ExtractedJob` entries, `classify_email()` returns immediately and never asks Gemini to extract company, title, location, or description. The resulting staged candidates therefore contain only platform/URL data, are materialized as blank jobs, and are rejected before evaluation.

LinkedIn emails also contain multiple tracking links for the same job. The current URL-based candidate key treats those tracking variants as different jobs.

Production state already contains legacy blank `gmail:linkedin` candidates/jobs, so future-only behavior changes are insufficient.

## Design

### 1. Deterministic detection remains authoritative for LinkedIn job alerts

`classify_deterministically()` continues to identify LinkedIn job-alert messages and extract safe job links. Stable LinkedIn job IDs are parsed from `/jobs/view/<id>` paths and attached to deterministic candidates.

Repeated LinkedIn URLs carrying the same job ID are collapsed to one deterministic candidate.

### 2. All deterministic `JOB_ALERT` messages receive semantic enrichment

A deterministic `JOB_ALERT` no longer returns before semantic classification. Gemini is called with the existing job-alert extraction instruction to populate company, title, location, remote status, description, and any other email-evidenced metadata.

The deterministic job URL/job ID remains the trusted identity. Semantic output enriches that identity rather than replacing it.

Technical Gemini/parser failures remain retryable sync errors, preserving the behavior established by the previous Gmail fix.

### 3. LinkedIn candidate identity is job-ID based

`source_candidate_key()` uses `id:linkedin:<job-id>` whenever a LinkedIn job ID can be obtained from either `source_job_id` or the URL. This prevents tracking-query variants of the same LinkedIn posting from becoming separate candidates.

Other platforms retain their existing identity behavior.

### 4. Reconciliation preserves enriched metadata

When semantic output and deterministic LinkedIn links refer to the same LinkedIn job ID, they are treated as the same candidate even if the URL strings differ. The semantic company/title/location/description are preserved, while the deterministic email URL/job ID supplies trusted identity.

### 5. Legacy blank LinkedIn artifacts are released for reprocessing

At writable Gmail sync startup, the store removes only legacy artifacts matching all of these characteristics:

- Gmail-origin inbound candidate with `source_platform='linkedin'`
- both company and title are empty
- corresponding materialized job is `source='gmail:linkedin'` with both company and title empty and has no dependent evaluation/material/delivery/application-event records
- corresponding Gmail message is classified `JOB_ALERT`

Those Gmail message records are removed so the normal backfill can classify them again. If backfill had previously completed, it is reopened exactly as with the existing legacy semantic-failure cleanup.

No non-blank LinkedIn jobs, non-LinkedIn jobs, or application lifecycle events are removed.

## Acceptance Criteria

1. A real-style LinkedIn alert containing `/comm/jobs/view/4461012343/?tracking...` calls semantic extraction and returns a `JOB_ALERT` candidate with non-empty semantic company/title metadata.
2. Multiple LinkedIn tracking URLs for job `4461012343` produce one candidate with key `id:linkedin:4461012343`.
3. A semantically normalized LinkedIn URL and the original tracked email URL reconcile into one enriched candidate.
4. Legacy blank LinkedIn Gmail candidates/jobs are narrowly removed and their messages become eligible for reprocessing.
5. Existing non-LinkedIn classification and candidate identity behavior remains unchanged.
6. Full test suite passes.

## Out of Scope

- Fetching LinkedIn pages directly.
- Changing job-ranking/evaluation policy.
- Broad Gmail query changes.
- Migrating SQLite state to Supabase.

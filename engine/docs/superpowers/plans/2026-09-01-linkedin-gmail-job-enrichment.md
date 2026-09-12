# LinkedIn Gmail Job Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure LinkedIn job-alert emails become deduplicated, metadata-rich Gmail job candidates that can reach the normal evaluation pipeline, while safely reprocessing legacy blank LinkedIn artifacts.

**Architecture:** Keep deterministic LinkedIn alert detection as the trusted identity layer, then run semantic extraction to enrich each alert. Reconcile deterministic and semantic candidates by LinkedIn job ID, use job-ID candidate keys, and narrowly clean legacy blank Gmail LinkedIn state before sync.

**Tech Stack:** Python 3.12, dataclasses, SQLite, pytest, GitHub Actions CI.

**Spec:** `docs/superpowers/specs/2026-09-01-linkedin-gmail-job-enrichment-design.md`

## Global Constraints

- Do not fetch LinkedIn pages directly.
- Do not change ranking/evaluation policy or Gmail query scope.
- Preserve technical semantic failures as retryable sync errors.
- Cleanup must target only blank Gmail-origin LinkedIn job-alert artifacts and must not delete dependent application data.

---

### Task 1: LinkedIn alert enrichment and stable identity

**Files:**
- Modify: `src/job_hunter/gmail_classifier.py`
- Test: `tests/test_gmail_classifier.py`

**Interfaces:**
- Consumes: `GmailMessage`, `GmailClassification`, `ExtractedJob`, `GeminiClient.generate_text()`.
- Produces: deterministic LinkedIn candidates with `source_job_id`; `source_candidate_key(job)` returning `id:linkedin:<job-id>` for LinkedIn; `classify_email()` enriched `JOB_ALERT` results.

- [ ] **Step 1: Add failing regression tests**

Add tests covering a real-style LinkedIn `/comm/jobs/view/4461012343/?tracking...` alert that verifies Gemini is called and semantic company/title metadata is retained, plus duplicate tracking URLs that verify one candidate and key `id:linkedin:4461012343`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `pytest tests/test_gmail_classifier.py -q`

Expected: the new enrichment/dedup tests fail because deterministic LinkedIn alerts currently bypass Gemini and URL variants remain separate.

- [ ] **Step 3: Implement minimal LinkedIn identity/enrichment logic**

Add a helper that parses a stable numeric LinkedIn ID from any path containing `/jobs/view/<id>`. Use it when building deterministic jobs to set `source_job_id` and deduplicate repeated URLs by job ID. Change deterministic `JOB_ALERT` handling so semantic extraction always runs. Update `source_candidate_key()` to prefer `id:linkedin:<id>` for LinkedIn only.

- [ ] **Step 4: Reconcile semantic and deterministic LinkedIn candidates by job ID**

When a semantic job represents the same LinkedIn ID as a deterministic email link, preserve semantic metadata and supply the deterministic source platform/job ID/email URL. Do not append a second bare deterministic job for the same ID.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `pytest tests/test_gmail_classifier.py -q`

Expected: all classifier tests pass.

- [ ] **Step 6: Commit**

Commit message: `fix: enrich LinkedIn Gmail job alerts`

---

### Task 2: Release legacy blank LinkedIn state

**Files:**
- Modify: `src/job_hunter/store.py`
- Modify: `src/job_hunter/gmail_sync.py`
- Test: `tests/test_store.py`
- Test: `tests/test_gmail_sync.py`

**Interfaces:**
- Produces: `JobStore.release_legacy_blank_linkedin_jobs() -> int`, returning the number of Gmail messages released for reprocessing.
- Consumes: existing Gmail sync-state reset behavior used by `release_legacy_gmail_semantic_failures()`.

- [ ] **Step 1: Add failing store cleanup tests**

Create legacy rows containing blank Gmail LinkedIn inbound candidates, blank materialized `gmail:linkedin` jobs, and processed `JOB_ALERT` Gmail messages. Assert the cleanup removes only those safe blank rows/message records while preserving non-blank LinkedIn jobs and unrelated Gmail records.

Also assert a blank LinkedIn job with dependent evaluation/material/delivery/application data is not deleted.

- [ ] **Step 2: Run focused store tests and verify RED**

Run: `pytest tests/test_store.py -q`

Expected: failure because `release_legacy_blank_linkedin_jobs()` does not exist.

- [ ] **Step 3: Implement narrowly guarded cleanup**

Delete blank LinkedIn inbound candidates. Delete only blank `gmail:linkedin` materialized jobs with no dependent rows. Remove processed `JOB_ALERT` Gmail message records corresponding to released blank candidates so they can be classified again. Return the count of released message IDs.

- [ ] **Step 4: Wire cleanup into writable Gmail sync startup**

Call the new cleanup beside the existing legacy semantic-failure cleanup. If either cleanup releases messages and backfill had completed, reopen backfill by clearing `backfill_completed_at` while preserving the current history/checkpoint fields.

- [ ] **Step 5: Add/adjust Gmail sync regression tests**

Verify writable sync invokes cleanup and reopens completed backfill when blank LinkedIn artifacts were released; dry-run must not mutate state.

- [ ] **Step 6: Run focused sync/store tests and verify GREEN**

Run: `pytest tests/test_store.py tests/test_gmail_sync.py -q`

Expected: all pass.

- [ ] **Step 7: Commit**

Commit message: `fix: reprocess blank LinkedIn Gmail jobs`

---

### Task 3: Full regression verification

**Files:**
- No production changes unless a regression exposes a required compatibility fix.

- [ ] **Step 1: Run full suite**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Review branch diff against `main`**

Confirm changes are limited to Gmail classifier/store/sync, their tests, and these design/plan docs.

- [ ] **Step 3: Verify CI on branch**

Push final branch state and confirm GitHub Actions CI succeeds before declaring completion.

# LinkedIn "Sign in" Legacy Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely release historical LinkedIn Gmail artifacts whose materialized job title is the login-page poison value `"Sign in"`, allowing those messages to be reprocessed by the corrected enrichment pipeline.

**Architecture:** Keep all behavior inside the existing `gmail_linkedin_cleanup` boundary. Introduce a small private predicate for legacy-poison detection and use it in the current dependency-guarded cleanup loop. Do not change global title validation or the Gmail classifier.

**Tech Stack:** Python 3.12, SQLite, pytest

**Spec:** `docs/superpowers/specs/2026-09-01-linkedin-sign-in-cleanup-design.md`

## Global Constraints

- Only `gmail:linkedin` legacy cleanup behavior may change.
- A non-empty company always blocks cleanup.
- Only blank titles and case-insensitive trimmed `"sign in"` titles count as poisoned.
- Existing dependency checks for evaluations, materials, deliveries, and application events must remain authoritative.
- Dry-run behavior must remain read-only.

---

### Task 1: Lock the production regression with tests

**Files:**
- Modify: `tests/test_linkedin_gmail_enrichment.py`

**Interfaces:**
- Consumes: `release_legacy_blank_linkedin_jobs(store) -> int`
- Produces: regression coverage for `"Sign in"` cleanup safety

- [ ] **Step 1: Extend the LinkedIn seed helper so candidate metadata and materialized job metadata can differ**

Add optional `job_company` and `job_title` parameters to `_seed_linkedin_candidate()`. Keep current callers unchanged by defaulting them to the candidate `company`/`title` values.

- [ ] **Step 2: Add a failing test for the real production poison value**

Create a test that seeds a blank LinkedIn inbound candidate but a corresponding `gmail:linkedin` job with blank company and title `"  SIGN IN  "`. Assert `release_legacy_blank_linkedin_jobs()` returns `1`, removes the Gmail processed marker, and deletes the materialized job.

- [ ] **Step 3: Add failing safety tests**

Add one test where the `"Sign in"` job has an evaluation dependency and assert it is preserved. Add one test where the materialized job has `company="Example"` and `title="Sign in"` and assert it is preserved.

- [ ] **Step 4: Run focused tests and confirm RED**

Run:

```bash
pytest -q tests/test_linkedin_gmail_enrichment.py
```

Expected: the new safe-release test fails because the current cleanup treats any non-empty title as populated. Existing tests should continue to pass.

- [ ] **Step 5: Commit the regression tests**

```bash
git add tests/test_linkedin_gmail_enrichment.py
git commit -m "test: cover LinkedIn sign-in legacy poison"
```

---

### Task 2: Recognize `"Sign in"` only inside guarded legacy cleanup

**Files:**
- Modify: `src/job_hunter/gmail_linkedin_cleanup.py`
- Test: `tests/test_linkedin_gmail_enrichment.py`

**Interfaces:**
- Produces: `_is_legacy_poisoned_linkedin_job(company: str, title: str) -> bool`
- Consumed by: `release_legacy_blank_linkedin_jobs(store) -> int`

- [ ] **Step 1: Add the minimal poison predicate**

Implement:

```python
def _is_legacy_poisoned_linkedin_job(company: str, title: str) -> bool:
    if company.strip():
        return False
    normalized_title = title.strip().casefold()
    return normalized_title in {"", "sign in"}
```

- [ ] **Step 2: Replace the current populated-job check**

Inside `release_legacy_blank_linkedin_jobs()`, replace the condition that rejects any non-empty company/title with a call to `_is_legacy_poisoned_linkedin_job(job["company"], job["title"])`. If the predicate returns `False`, mark the message unsafe and preserve it. Leave `_job_has_dependencies()` unchanged.

- [ ] **Step 3: Run focused tests and confirm GREEN**

Run:

```bash
pytest -q tests/test_linkedin_gmail_enrichment.py
```

Expected: all focused LinkedIn Gmail enrichment/cleanup tests pass.

- [ ] **Step 4: Run the full suite**

Run:

```bash
pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 5: Commit the implementation**

```bash
git add src/job_hunter/gmail_linkedin_cleanup.py tests/test_linkedin_gmail_enrichment.py
git commit -m "fix: release LinkedIn sign-in legacy artifacts"
```

---

### Task 3: Verify branch scope

**Files:**
- No new runtime files

- [ ] **Step 1: Compare the branch to `main`**

Confirm the only runtime change is `src/job_hunter/gmail_linkedin_cleanup.py`, with focused test changes plus the required spec/plan docs.

- [ ] **Step 2: Verify the full test result from the final commit**

Use a fresh CI or local `pytest -q` run against the final branch head and record the exact pass/fail count before opening a PR.

# Missing Stack Test Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an accidental Job Hunter run without the local Supabase stack fail clearly while preserving explicit non-database runs.

**Architecture:** The pytest plugin in `tests/conftest.py` validates the three required variables once in `pytest_sessionstart`. A CLI flag and an environment variable opt out only when configuration is entirely absent; the existing `_stack_env` fixture then performs the deliberate skips.

**Tech Stack:** Python 3.12+, pytest 8+, pytester

**Spec:** `apps/job-hunter/docs/superpowers/specs/2026-09-09-missing-stack-test-guard-design.md`

## Global Constraints

- `JOB_HUNTER_ALLOW_MISSING_STACK=1` and `--allow-missing-stack` are equivalent opt-outs.
- An opt-out is valid only when none of `SUPABASE_TEST_URL`, `SUPABASE_TEST_PUBLISHABLE_KEY`, and `SUPABASE_TEST_SIGNING_KEY_B64` is set.
- Partial configuration always fails once at session start and names the missing variables.
- `SUPABASE_TEST_DB_URL` remains optional and outside this guard.
- Tests assert store-backed-test behavior, not a global zero-skip count.

---

### Task 1: Pin the pytest-session contract

**Files:**
- Create: `apps/job-hunter/tests/test_stack_environment_guard.py`
- Modify: `apps/job-hunter/tests/conftest.py`

**Interfaces:**
- Consumes: `_REQUIRED` and `_stack_env` from `tests.conftest`
- Produces: `pytest_addoption(parser)`, `pytest_sessionstart(session)`, and the `--allow-missing-stack` option

- [ ] **Step 1: Write failing pytester tests**

Create nested pytest sessions containing one ordinary test and one test requesting `_stack_env`.
Cover default absence, both opt-outs, partial configuration under both opt-outs, and complete
configuration. Assert exit codes, one clear error, missing-variable names, and probe outcomes.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
JOB_HUNTER_ALLOW_MISSING_STACK=1 pnpm job-hunter:test tests/test_stack_environment_guard.py
```

Expected: failures because `--allow-missing-stack` and the session-start guard do not exist.

- [ ] **Step 3: Implement the minimal guard**

In `tests/conftest.py`, register the flag and validate configuration from
`pytest_sessionstart`. Raise one `pytest.UsageError` for absent accidental runs and every partial
configuration. Let a complete environment continue unchanged. Let a fully absent environment
continue only when the flag is set or `JOB_HUNTER_ALLOW_MISSING_STACK` equals `1`; `_stack_env`
then skips the store-backed tests deliberately.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command again. Expected: all guard tests pass; no database access is required.

- [ ] **Step 5: Run the mutation check**

Confirm the tests would fail if the partial-configuration branch honored the opt-out, if the
environment opt-out accepted a value other than `1`, if the error omitted a missing variable,
or if complete configuration skipped the probe store-backed test.

### Task 2: Replace the obsolete manual invariant

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: the enforced behavior from Task 1
- Produces: one accurate test-guidance section extending the text introduced by PR #206

- [ ] **Step 1: Update the existing guidance in place**

Replace the opening paragraphs under “A green Job Hunter run is only evidence if the store tests
ran.” Document the default one-time failure, the environment and CLI opt-outs, the rejection of
partial configuration, and why unrelated skip counts are not the invariant.

- [ ] **Step 2: Verify the documented commands against the harness**

Run one nested guard test for each opt-out and the complete-environment case through the focused
test file. Expected: both deliberate opt-outs work and the complete probe is not skipped.

### Task 3: Verify the complete change

**Files:**
- Verify: `apps/job-hunter/tests/conftest.py`
- Verify: `apps/job-hunter/tests/test_stack_environment_guard.py`
- Verify: `AGENTS.md`

**Interfaces:**
- Consumes: Tasks 1 and 2
- Produces: fresh evidence for issue #208's acceptance criteria

- [ ] **Step 1: Reproduce the original command without configuration**

Run the focused nested-session regression that clears all required variables. Expected: exit 4,
one usage error, and no successful test summary.

- [ ] **Step 2: Run the full configured Job Hunter suite**

Run `pnpm job-hunter:test` with the local stack environment exported. Expected: exit 0, with no
store-backed test skipped because the three required variables are missing. Do not require a
global zero-skip count.

- [ ] **Step 3: Review the exact diff and repository state**

Confirm only the approved guard, its tests, the existing guidance, and these repository-required
design/plan documents changed. Confirm the branch remains `fix/job-hunter-missing-stack`.

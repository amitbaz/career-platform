# Effective Match Score Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Gemini reports that a job has a core (`must_have`, non-`familiarity`) requirement the candidate cannot support, deterministically cap the user-facing match score below the configured `possible` threshold, preserving Gemini's raw component sum separately for diagnostics.

**Architecture:** `Evaluation.total_score` keeps its existing meaning of "the score we stand behind" and gains a deterministic cap applied in `evaluate_job` before the decision ladder runs. Gemini's untouched sum moves to a new `Evaluation.raw_model_score` field, persisted in a new `evaluations.raw_model_score` column that is backfilled from `total_score` on startup. No display, sort, digest, or pipeline code changes — they all already read `total_score`.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-04-effective-match-score-design.md`

## Global Constraints

- No additional Gemini calls. The cap is deterministic post-processing of the existing evaluation result.
- No new external dependencies.
- Do not weaken the existing score-component validation in `evaluation.py:277-288`.
- Do not change `HIGH_PRIORITY_THRESHOLD` (module constant, `evaluation.py:23`).
- Do not change the three delivery-floor constants (`telegram.py:25` = 60, `store.py:287` = 60, `pipeline.py:66` = 61). Unifying them is explicitly out of scope.
- Do not change `select_deliverable_items` (`telegram.py:47-58`). Capped jobs becoming invisible is the intended outcome.
- Do not touch the `insufficient_content` / content-confidence path beyond the refactor in Task 2.
- Persistence stays SQLite. Do not introduce Supabase.
- Cap value is always derived: `max(0, policy.thresholds.get("possible", 65) - 1)`.
- Run `.venv/bin/python -m pytest -q` (891 tests, currently green) before declaring done.

## Preflight

Before Task 1, create a branch off `main`:

```bash
git checkout -b fix/effective-match-score
```

Both files under `docs/superpowers/` (this plan and its spec) are already written and uncommitted. Commit them first:

```bash
git add docs/superpowers/specs/2026-09-04-effective-match-score-design.md docs/superpowers/plans/2026-09-04-effective-match-score.md
git commit -m "docs: design and plan for effective match score capping"
```

## File Structure

- `src/job_hunter/models.py` — add one field to the `Evaluation` dataclass (`models.py:141-157`). Responsibility unchanged: plain domain records.
- `src/job_hunter/evaluation.py` — add the cap helper and apply it in `evaluate_job` (`:252-333`). Responsibility unchanged: turn a Gemini response into a validated `Evaluation`.
- `src/job_hunter/store.py` — new column dict, migration + backfill call in `_init_db` (`:327-349`), one field added to the `save_evaluation` INSERT (`:1887-1915`) and the `get_evaluation` SELECT (`:2011-2035`).
- `tests/test_evaluation.py` — modify one existing test, add four.
- `tests/test_store.py` — add two tests (round-trip, backfill).
- `tests/test_pipeline.py` — add one end-to-end test proving a capped job never reaches Telegram.

No new files. No file is large enough to warrant splitting.

---

### Task 1: Add `raw_model_score` to the `Evaluation` model

**Files:**
- Modify: `src/job_hunter/models.py:141-157`
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Evaluation.raw_model_score: int` (defaults to `0`), the last field of the dataclass. Tasks 2 and 3 both rely on this name and type.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_evaluation.py`:

```python
def test_evaluation_defaults_raw_model_score_to_zero():
    evaluation = Evaluation(
        job_id=1,
        total_score=70,
        scores={},
        decision="possible_match",
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="m",
    )
    assert evaluation.raw_model_score == 0
```

`Evaluation` is not currently imported in this file. Extend the existing line at `tests/test_evaluation.py:7` — `from job_hunter.models import CandidateContext, CandidatePreferences, Job, SearchPolicy` — to include `Evaluation` (alphabetically, after `CandidatePreferences`). Do not add a second import statement.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_evaluation.py::test_evaluation_defaults_raw_model_score_to_zero -v`
Expected: FAIL with `AttributeError: 'Evaluation' object has no attribute 'raw_model_score'`

- [ ] **Step 3: Write minimal implementation**

In `src/job_hunter/models.py`, add one line as the **last** field of the `Evaluation` dataclass, directly after `requirements`:

```python
@dataclass(slots=True)
class Evaluation:
    job_id: int
    total_score: int
    scores: dict
    decision: str
    hard_blockers: list
    strengths: list
    gaps: list
    salary_note: str
    location_note: str
    rationale: str
    model: str
    status: str = "ok"
    market_id: str = ""
    content_confidence: str = ""
    requirements: dict = field(default_factory=dict)
    #: Gemini's raw component sum, before any deterministic cap. Diagnostics
    #: only — `total_score` is the number every consumer should use.
    raw_model_score: int = 0
```

It must go last and must have a default, because `slots=True` dataclasses still require defaulted fields to follow non-defaulted ones.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_evaluation.py::test_evaluation_defaults_raw_model_score_to_zero -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to confirm nothing broke**

Run: `.venv/bin/python -m pytest -q`
Expected: 892 passed (891 existing + 1 new)

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/models.py tests/test_evaluation.py
git commit -m "feat: add raw_model_score field to Evaluation"
```

---

### Task 2: Cap the score in `evaluate_job`

**Files:**
- Modify: `src/job_hunter/evaluation.py:300-333`
- Test: `tests/test_evaluation.py:400-434`

**Interfaces:**
- Consumes: `Evaluation.raw_model_score` from Task 1.
- Produces: module-level `_capped_score(total: int, possible_threshold: int) -> int`. Nothing outside `evaluation.py` calls it; it exists so the cap arithmetic is stated once and testable.

Background for the implementer: `evaluate_job` already computes `total` (the validated component sum), `must_have` (a validated list of `{"requirement", "depth", "candidate_support"}` dicts), and `major_unsupported_must_have` (a bool). The decision ladder immediately below picks `blocked` / `high_priority` / `package_match` / `possible_match` / `skip` from `total`. This task inserts the cap between those two blocks.

- [ ] **Step 1: Modify the existing regression test**

`tests/test_evaluation.py:400-410` currently locks in the old behaviour. Replace the whole function with:

```python
def test_major_unsupported_must_have_caps_score_below_possible(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.raw_model_score == 89
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.total_score < policy.thresholds["possible"]
    assert evaluation.decision == "skip"
```

This is an intentional behaviour change, not a bug fix: the old test asserted `total_score == 89` and `decision == "possible_match"`. Say so in the commit message.

- [ ] **Step 2: Add the carve-out and precedence tests**

Append to `tests/test_evaluation.py`:

```python
def test_experience_depth_unsupported_must_have_is_also_capped(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "End-to-end forecasting", "depth": "experience", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.decision == "skip"


def test_partial_support_must_have_is_not_capped(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "candidate_support": "partial"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.raw_model_score == 89
    assert evaluation.decision == "high_priority"


def test_hard_blocker_takes_precedence_over_cap(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89, hard_blockers=["Requires on-site in the US"])
    payload["requirements"]["must_have"] = [
        {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "blocked"
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.raw_model_score == 89


def test_uncapped_evaluation_keeps_raw_and_total_in_sync(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.raw_model_score == 89
```

- [ ] **Step 3: Extend the two existing carve-out tests with score assertions**

`tests/test_evaluation.py:412-419` (`test_familiarity_depth_unsupported_must_have_does_not_gate`) and `:422-428` (`test_unsupported_preferred_requirement_does_not_gate`) currently assert only the decision. Add one line to each, immediately after the existing decision assertion:

```python
    assert evaluation.total_score == 89
```

Do not otherwise alter these two tests.

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_evaluation.py -q`
Expected: FAIL — `test_major_unsupported_must_have_caps_score_below_possible` fails with `assert 89 == 64`, and the `raw_model_score` assertions fail with `assert 0 == 89`.

- [ ] **Step 5: Add the cap helper**

In `src/job_hunter/evaluation.py`, add this module-level function directly above `def evaluate_job(...)` (which starts at `:252`):

```python
def _capped_score(total: int, possible_threshold: int) -> int:
    """Lower `total` so it cannot sit in the `possible_match` band or above.

    Applied when the candidate has no support for a core requirement. The cap
    is derived from configuration rather than hardcoded so it tracks
    `policy.thresholds["possible"]`; `max(0, ...)` guards a threshold of 0.
    """
    return min(total, max(0, possible_threshold - 1))
```

- [ ] **Step 6: Apply the cap in `evaluate_job`**

Replace `evaluation.py:300-316` — from the `major_unsupported_must_have = any(` line through the `decision = "skip"` line — with:

```python
    major_unsupported_must_have = any(
        item["candidate_support"] == "unsupported" and item["depth"] != "familiarity"
        for item in must_have
    )
    insufficient_content = not content_confidence.is_sufficient(job.content_confidence)
    # A major unsupported must-have no longer needs to gate the decision ladder:
    # capping the score below `possible` already puts it out of reach of the
    # `package_match` and `high_priority` rungs. Thin postings still gate here,
    # because failing to read a description is not evidence of a poor fit.
    confident_decision_available = not insufficient_content

    possible_threshold = policy.thresholds.get("possible", 65)
    raw_total = total
    if major_unsupported_must_have:
        total = _capped_score(total, possible_threshold)

    if hard_blockers:
        decision = "blocked"
    elif total >= HIGH_PRIORITY_THRESHOLD and confident_decision_available:
        decision = "high_priority"
    elif total >= policy.thresholds.get("package", 75) and confident_decision_available:
        decision = "package_match"
    elif total >= possible_threshold:
        decision = "possible_match"
    else:
        decision = "skip"
```

Then in the `return Evaluation(...)` block below (`:318-333`), add one argument after `requirements=...`:

```python
        requirements={"must_have": must_have, "preferred": preferred},
        raw_model_score=raw_total,
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_evaluation.py -q`
Expected: all pass.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. If `tests/test_pipeline.py` or `tests/test_store.py` fail here, a fixture is feeding an unsupported deep must-have into a test that expects a high score — read the failure and report it before changing anything outside this task's file list.

- [ ] **Step 9: Commit**

```bash
git add src/job_hunter/evaluation.py tests/test_evaluation.py
git commit -m "feat: cap match score below possible threshold for unsupported must-haves

A must_have requirement the candidate cannot support, at experience or
deep_expert depth, now lowers total_score to one below the configured
possible threshold, so the decision ladder yields skip. Gemini's raw
component sum is preserved on Evaluation.raw_model_score.

Intentional behaviour change: the previous regression test asserted the
score stayed at 89 with a possible_match decision. Closes #45 (evaluation
half)."
```

---

### Task 3: Persist and round-trip `raw_model_score`

**Files:**
- Modify: `src/job_hunter/store.py` — near `:122` (column dicts), `:327-349` (`_init_db`), `:1887-1915` (`save_evaluation`), `:2011-2035` (`get_evaluation`)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Evaluation.raw_model_score` from Task 1, populated by Task 2.
- Produces: `evaluations.raw_model_score INTEGER NOT NULL DEFAULT 0`, backfilled from `total_score`. No new public method.

Background: `JobStore` has no versioned migration system. `_init_db` runs `CREATE TABLE IF NOT EXISTS` for each table, then `_add_missing_columns(table, columns)` which reads `PRAGMA table_info` and issues `ALTER TABLE ... ADD COLUMN` for anything absent. That is additive only, so a plain default of `0` would leave every pre-existing evaluation reading back as `raw_model_score = 0`. Hence the backfill.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
def test_evaluation_raw_model_score_round_trip(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id, _, _ = store.upsert_job(Job(source="x", source_job_id="1", title="Analyst", company="Acme"))
    store.save_evaluation(job_id, _evaluation(job_id, total_score=64, raw_model_score=89))
    loaded = store.get_evaluation(job_id)
    assert loaded.total_score == 64
    assert loaded.raw_model_score == 89


def test_legacy_evaluation_rows_backfill_raw_model_score(tmp_path):
    db_path = tmp_path / "state.sqlite3"
    store = JobStore(db_path)
    job_id, _, _ = store.upsert_job(Job(source="x", source_job_id="1", title="Analyst", company="Acme"))
    store.save_evaluation(job_id, _evaluation(job_id, total_score=77, raw_model_score=77))
    # Simulate a row written before the column existed.
    with store._conn:
        store._conn.execute("UPDATE evaluations SET raw_model_score = 0")
    store._conn.close()

    reopened = JobStore(db_path)
    assert reopened.get_evaluation(job_id).raw_model_score == 77
```

Notes for the implementer: `_evaluation(job_id, **overrides)` is the existing helper at `tests/test_store.py:44-59`; it passes overrides straight to the `Evaluation` constructor, so `raw_model_score=` works with no change to it. There is **no** `_job()` factory in this file — store tests build `Job(...)` inline, and `upsert_job` returns a **3-tuple**, so the `job_id, _, _ =` unpacking is required (see `test_get_evaluation_and_material_roundtrip` at `:846-849` for the pattern). `JobStore` accepts a `Path`, so `tmp_path / "state.sqlite3"` needs no `str()`. Both tests use `tmp_path` rather than `":memory:"` because the second closes and reopens the database to re-trigger `_init_db`. `Job` and `Evaluation` are already imported at `tests/test_store.py:10`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_store.py -k raw_model_score -v`
Expected: FAIL — `sqlite3.OperationalError: table evaluations has no column named raw_model_score`, or an `AttributeError` from the `Evaluation` constructor path.

- [ ] **Step 3: Declare the column**

In `src/job_hunter/store.py`, directly below `_CONTENT_TRUST_EVALUATION_COLUMNS` (`:122`):

```python
_RAW_SCORE_EVALUATION_COLUMNS = {"raw_model_score": "INTEGER NOT NULL DEFAULT 0"}
```

- [ ] **Step 4: Add the migration and backfill**

In `_init_db`, immediately after the existing `self._add_missing_columns("evaluations", _CONTENT_TRUST_EVALUATION_COLUMNS)` line:

```python
            self._add_missing_columns("evaluations", _RAW_SCORE_EVALUATION_COLUMNS)
            self._backfill_raw_model_score()
```

Then add this method next to the other migration helpers (below `_migrate_jobs_to_r3_schema`, around `:355`):

```python
    def _backfill_raw_model_score(self) -> None:
        """Give pre-existing evaluation rows a meaningful raw score.

        `_add_missing_columns` can only ALTER TABLE ... ADD COLUMN with a fixed
        default, so rows written before `raw_model_score` existed would read
        back as 0. Before the cap was introduced no score was ever adjusted, so
        their raw score equalled their total. Idempotent and cheap: a genuine
        all-zero evaluation has total_score = 0, making the update a no-op.
        """
        self._conn.execute(
            "UPDATE evaluations SET raw_model_score = total_score WHERE raw_model_score = 0"
        )
```

- [ ] **Step 5: Write and read the column**

In `save_evaluation` (`:1887-1915`), add `raw_model_score` to the column list, add one more `?` to the `VALUES` tuple, and add `evaluation.raw_model_score,` to the parameter tuple in the matching position. The column list and the placeholder count must stay in sync — count them.

In `get_evaluation` (`:2011-2035`), add `raw_model_score` to the `SELECT` list and `raw_model_score=row["raw_model_score"],` to the `Evaluation(...)` construction.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_store.py -k raw_model_score -v`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. The existing `evaluations` schema test at `tests/test_store.py:120` asserts column *membership*, not an exact set, so it is unaffected.

- [ ] **Step 8: Commit**

```bash
git add src/job_hunter/store.py tests/test_store.py
git commit -m "feat: persist raw_model_score with backfill for legacy evaluations"
```

---

### Task 4: Prove a capped job never reaches Telegram

**Files:**
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3. Produces nothing.

This task adds no production code. It exists because the user-visible outcome of this change — the job disappearing from both the digest and the card navigator — is a consequence of code in three separate modules, and nothing currently asserts it end to end.

Background: `select_deliverable_items` (`src/job_hunter/telegram.py:47-58`) drops any item whose score is at or below 60 **and** any item whose decision is `skip`. The pipeline builds both the digest text (`pipeline.py:742`) and the navigation session (`pipeline.py:785`) from that same filtered list. A capped job scores 64, which clears the numeric floor — so it is the `skip` decision, not the score, that removes it. That distinction is what this test pins down.

- [ ] **Step 1: Write the test**

Append to `tests/test_pipeline.py`:

```python
def test_capped_job_is_excluded_from_digest_and_navigation():
    capped = DigestItem(
        job_id=1,
        company="Forecast GmbH",
        title="Product Analytics Lead",
        score=64,
        decision="skip",
        url="https://example.test/jobs/1",
        hard_blockers=[],
    )
    plausible = DigestItem(
        job_id=2,
        company="Example GmbH",
        title="Senior Frontend Engineer",
        score=70,
        decision="possible_match",
        url="https://example.test/jobs/2",
        hard_blockers=[],
    )

    deliverable = select_deliverable_items([capped, plausible])

    assert [item.job_id for item in deliverable] == [2]
    assert "Forecast GmbH" not in build_digest([capped, plausible])
```

Two imports must be extended in `tests/test_pipeline.py`. Add `DigestItem` to the multi-line `from job_hunter.models import (...)` block at `:12-24` (keep it alphabetical — it goes between `CompanyWatchSeed` and `Evaluation`). Change the single-name telegram import at `:29` from `from job_hunter.telegram import build_gemini_pause_warning` to also pull in `build_digest` and `select_deliverable_items`. `DigestItem` itself is defined at `src/job_hunter/models.py:328-338`.

- [ ] **Step 2: Run the test**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py::test_capped_job_is_excluded_from_digest_and_navigation -v`
Expected: PASS immediately — this asserts existing behaviour that Tasks 1-3 now depend on. If it fails, the filter does not behave as the spec assumes; stop and report before proceeding.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_pipeline.py
git commit -m "test: pin that capped skip-decision jobs never reach digest or navigator"
```

---

### Task 5: Verify the acceptance criteria and open the PR

**Files:** none modified.

- [ ] **Step 1: Walk the issue's acceptance criteria**

Confirm each against a real test, by name:

| Criterion | Covered by |
|---|---|
| Raw 89 + `unsupported` `deep_expert` must-have is no longer 89; effective score below `possible`; decision `skip` | `test_major_unsupported_must_have_caps_score_below_possible` |
| Raw 71 + central `experience`/`deep_expert` must-have cannot show as `Match: 71%` | `test_experience_depth_unsupported_must_have_is_also_capped` + `test_capped_job_is_excluded_from_delivery` |
| Unsupported *preferred* requirement still scores strongly and can be `package_match`/`high_priority` | `test_unsupported_preferred_requirement_does_not_gate` |
| Unsupported *familiarity*-depth must-have is not capped | `test_familiarity_depth_unsupported_must_have_does_not_gate` |
| Telegram card display and navigation sort both use the effective score | No code change needed — both read `total_score` (`telegram_navigation.py:52,69`), which now carries the capped value. State this explicitly in the PR body. |
| Raw score round-trips through persistence; consumers still default to the effective score | `test_evaluation_raw_model_score_round_trip`, `test_legacy_evaluation_rows_backfill_raw_model_score` |
| Salary/location/language/sponsorship blocker behaviour unchanged | `test_hard_blocker_takes_precedence_over_cap` plus the untouched existing blocker tests |
| Full test suite passes | Step 2 |

- [ ] **Step 2: Run the full suite one final time**

Run: `.venv/bin/python -m pytest -q`
Expected: 900 passed (891 existing + 9 new; one existing test was rewritten rather than added). Report the real number — do not assert this one is correct without seeing it.

- [ ] **Step 3: Open the PR**

```bash
git push -u origin fix/effective-match-score
gh pr create --title "Cap user-facing match score for unsupported must-have requirements" --body "$(cat <<'EOF'
Closes #45.

A `must_have` requirement the candidate cannot support, at `experience` or
`deep_expert` depth, now lowers the job's `total_score` to one below the
configured `possible` threshold. The decision ladder therefore yields `skip`,
and the existing Telegram filter drops the job from both the digest and the
card navigator.

Gemini's untouched component sum is preserved on `Evaluation.raw_model_score`
and in a new `evaluations.raw_model_score` column, backfilled from
`total_score` for rows written before the column existed.

No display or sort code changed: `Match: X%` and the navigation sort key
already read `total_score`, which now carries the capped value.

Intentional behaviour change: `test_major_unsupported_must_have_caps_below_high_priority`
previously asserted the score stayed at 89 with a `possible_match` decision.
It has been rewritten.

Out of scope, unchanged: score component weights, the content-confidence
downgrade path, the three separately-defined delivery-floor constants.

Design: `docs/superpowers/specs/2026-09-04-effective-match-score-design.md`
Plan: `docs/superpowers/plans/2026-09-04-effective-match-score.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01UN4AeS27sKb9ohAMsRY9CC
EOF
)"
```

---

## Self-Review

**Spec coverage:** Decision 1 (cap in place + raw beside it) → Tasks 1, 2. Decision 2 (capped jobs vanish) → Task 4. Decision 3 (backfill) → Task 3. Decision 4 (config-derived cap) → Task 2 Step 5. Decision 5 (collapse the redundant gate) → Task 2 Step 6. Decision 6 (thin content untouched) → enforced by Global Constraints, asserted by the untouched `test_insufficient_content_confidence_caps_below_high_priority`. Decision 7 (no recompute of history) → no task, by design. Every carve-out row in the spec's table maps to a named test in Task 5's table.

**Placeholder scan:** No TBDs. Every code step carries real code. The two spots that name rather than quote — the `save_evaluation` INSERT and `get_evaluation` SELECT edits in Task 3 Step 5, and the `_job()` factory lookup in Task 3 Step 1 — are mechanical list edits in long literals where quoting the full statement would invite a stale copy; both name the exact file, line range, and what to add.

**Type consistency:** `raw_model_score: int` is used identically in `models.py`, `evaluation.py` (`raw_model_score=raw_total`), `store.py` (both directions), and all tests. `_capped_score(total, possible_threshold)` is defined once and called once. `possible_threshold` is read once and reused for both the cap and the `possible_match` rung, so the two can never drift.

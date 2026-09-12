# Job Hunter Bot v2.1 Candidate Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop off-target professions from consuming Gemini calls, raise the evaluation safety ceiling to 75, and make Telegram show only deliverable decisions sorted by final Gemini score.

**Architecture:** Extend the deterministic prefilter with a configuration-driven software/product-engineering profession gate and expose profession-rejection counts through discovery statistics. Keep v2 discovery and ranking intact, but apply the higher 75-job ceiling after the stricter gate. Make Telegram delivery explicitly filter supported decisions and sort final results by Gemini score, while preserving SQLite state and retry behavior.

**Tech Stack:** Python 3.12, PyYAML, SQLite, pytest, existing requests/Telegram/Gemini integrations.

**Spec:** `docs/superpowers/specs/2026-08-30-job-hunter-bot-v2-1-candidate-quality-design.md`

## Global Constraints

- Do not change the Gemini model.
- Do not change Gemini scoring weights or thresholds.
- Do not change v2 discovery-source coverage.
- Keep existing SQLite artifacts compatible without reset or destructive migration.
- Preserve current evaluation caching, cover-letter/PDF generation, Telegram retry, and scheduled-run behavior.
- A blocked/off-target profession phrase always wins over a generic engineering marker.
- Description keywords must not make a non-engineering title eligible for Gemini.
- The default `max_jobs_per_run` is 75 and remains configurable.
- `skip` and unknown decisions must never be delivered to Telegram.
- Sort every delivered Telegram section by final Gemini score descending with deterministic tie-breakers.
- Send ready-to-apply PDFs in the same score-descending order.
- Tests must not make live external calls.
- Run `pytest -q` before completion.

---

### Task 1: Add configuration-driven profession gating

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/config.py`
- Modify: `config/search.yml`
- Modify: `src/job_hunter/prefilter.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_prefilter.py`

**Interfaces:**
- Produces: `SearchPolicy.engineering_title_keywords: list[str]`
- Produces: `SearchPolicy.engineering_title_phrases: list[str]`
- Produces: `SearchPolicy.blocked_profession_title_phrases: list[str]`
- Produces: `PrefilterResult.reason_code: str`
- Produces: `is_software_engineering_title(title: str, policy: SearchPolicy) -> bool`

- [ ] **Step 1: Write failing config tests**

Extend `tests/test_config.py` so the YAML fixture includes:

```yaml
max_jobs_per_run: 75
engineering_title_keywords:
  - engineer
  - developer
engineering_title_phrases:
  - technical lead
  - frontend lead
  - software architect
blocked_profession_title_phrases:
  - product manager
  - product designer
  - sales engineer
  - data engineer
```

Assert:

```python
assert settings.policy.max_jobs_per_run == 75
assert settings.policy.engineering_title_keywords == ["engineer", "developer"]
assert settings.policy.engineering_title_phrases == [
    "technical lead",
    "frontend lead",
    "software architect",
]
assert settings.policy.blocked_profession_title_phrases == [
    "product manager",
    "product designer",
    "sales engineer",
    "data engineer",
]
```

- [ ] **Step 2: Run config tests and confirm failure**

```bash
pytest tests/test_config.py -q
```

Expected: failure because the new policy fields do not exist and the default run limit is still 25.

- [ ] **Step 3: Add policy fields and default limit**

In `models.py`, extend `PrefilterResult` and `SearchPolicy`:

```python
@dataclass(slots=True)
class PrefilterResult:
    should_evaluate: bool
    hard_blocker: bool
    reason: str
    reason_code: str = ""


@dataclass(slots=True)
class SearchPolicy:
    target_titles: list
    positive_keywords: list
    blocked_title_keywords: list
    salary_floor_eur: int
    thresholds: dict
    max_jobs_per_run: int = 75
    search_queries: list = field(default_factory=list)
    ats: dict = field(default_factory=dict)
    role_families: list[str] = field(default_factory=list)
    search_query_templates: list[str] = field(default_factory=list)
    search_domains: list[str] = field(default_factory=list)
    max_search_queries_per_run: int = 30
    engineering_title_keywords: list[str] = field(default_factory=lambda: ["engineer", "developer"])
    engineering_title_phrases: list[str] = field(default_factory=lambda: [
        "technical lead",
        "frontend lead",
        "front-end lead",
        "software lead",
        "engineering lead",
        "software architect",
        "frontend architect",
        "front-end architect",
        "web architect",
    ])
    blocked_profession_title_phrases: list[str] = field(default_factory=lambda: [
        "product manager",
        "platform product manager",
        "technical product manager",
        "product designer",
        "ux designer",
        "ui designer",
        "product marketing manager",
        "program manager",
        "project manager",
        "customer success manager",
        "solutions consultant",
        "sales engineer",
        "solutions engineer",
        "support engineer",
        "data engineer",
        "machine learning engineer",
        "ml engineer",
        "data scientist",
        "ml researcher",
        "machine learning researcher",
        "ios engineer",
        "android engineer",
        "mobile engineer",
        "embedded engineer",
    ])
```

In `config.py`, load all three lists and change the fallback to `max_jobs_per_run=75`. Use the dataclass defaults when keys are absent by copying the same lists into local module constants or a helper rather than sharing mutable list objects.

- [ ] **Step 4: Update production YAML**

Change:

```yaml
max_jobs_per_run: 75
```

and add the complete `engineering_title_keywords`, `engineering_title_phrases`, and `blocked_profession_title_phrases` lists from the spec.

- [ ] **Step 5: Write failing profession-gate tests**

Add these tests to `tests/test_prefilter.py` using a policy with the new fields populated:

```python
@pytest.mark.parametrize("title", [
    "Senior Product Engineer",
    "Staff Frontend Engineer",
    "Senior Software Engineer, Product",
    "Founding Engineer",
    "Frontend Developer",
    "Frontend Technical Lead",
])
def test_prefilter_accepts_software_engineering_professions(policy, title):
    result = prefilter_job(
        Job(source="x", title=title, description="React TypeScript product ownership", remote=True),
        policy,
    )
    assert result.should_evaluate is True
```

```python
@pytest.mark.parametrize("title", [
    "Senior Product Manager",
    "Platform Product Manager",
    "Technical Product Manager",
    "Senior Product Designer",
    "Product Designer, AI",
    "Senior Sales Engineer",
    "Senior Data Engineer",
    "Machine Learning Engineer",
    "Senior iOS Engineer",
])
def test_prefilter_rejects_off_target_professions_even_with_positive_keywords(policy, title):
    result = prefilter_job(
        Job(
            source="x",
            title=title,
            description="React TypeScript SaaS product ownership architecture",
            remote=True,
        ),
        policy,
    )
    assert result.should_evaluate is False
    assert result.reason_code == "off_target_profession"
```

Add a separate test proving an existing blocked title such as `Junior Frontend Engineer` returns `reason_code == "blocked_title"` before profession acceptance.

- [ ] **Step 6: Run prefilter tests and confirm failure**

```bash
pytest tests/test_prefilter.py -q
```

Expected: off-target titles currently pass because positive description keywords are sufficient.

- [ ] **Step 7: Implement title profession helper**

In `prefilter.py` add:

```python
def is_software_engineering_title(title: str, policy: SearchPolicy) -> bool:
    normalized = normalize_text(title or "")
    if not normalized:
        return False

    if any(normalize_text(phrase) in normalized for phrase in policy.blocked_profession_title_phrases):
        return False

    if any(normalize_text(phrase) in normalized for phrase in policy.engineering_title_phrases):
        return True

    return any(normalize_text(keyword) in normalized for keyword in policy.engineering_title_keywords)
```

Update `prefilter_job()` in this order:

```text
explicit non-remote
existing blocked_title_keywords
off-target/software-profession gate
target-title or positive-keyword relevance
```

Return stable reason codes:

```text
not_remote
blocked_title
off_target_profession
no_relevance
passed
```

The profession gate must run before description keyword relevance.

- [ ] **Step 8: Run focused tests**

```bash
pytest tests/test_config.py tests/test_prefilter.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/job_hunter/models.py src/job_hunter/config.py src/job_hunter/prefilter.py config/search.yml tests/test_config.py tests/test_prefilter.py
git commit -m "fix: gate Gemini evaluation to target engineering professions"
```

---

### Task 2: Track profession rejections and make 75 a visible safety ceiling

**Files:**
- Modify: `src/job_hunter/discovery.py`
- Modify: `src/job_hunter/pipeline.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Adds: `DiscoveryStats.profession_rejected: int`
- Preserves: `DiscoveryResult.eligible: list[tuple[int, Job]]`
- Preserves: global `rank_jobs()` ordering before selection.

- [ ] **Step 1: Write failing discovery-stat test**

Add a test where one valid engineering job and one Product Manager both contain positive keywords:

```python
def test_collect_candidates_counts_profession_rejections_separately(store, http, policy):
    jobs = [
        Job(
            source="x",
            source_job_id="eng-1",
            title="Senior Product Engineer",
            description="React TypeScript",
            remote=True,
        ),
        Job(
            source="x",
            source_job_id="pm-1",
            title="Senior Product Manager",
            description="React TypeScript SaaS product ownership",
            remote=True,
        ),
    ]
    result = collect_candidates([FakeSource(jobs)], store, http, policy)
    assert result.stats.profession_rejected == 1
    assert result.stats.prefilter_rejected == 0
    assert len(result.eligible) == 1
    assert result.eligible[0][1].title == "Senior Product Engineer"
```

- [ ] **Step 2: Run discovery test and confirm failure**

```bash
pytest tests/test_discovery.py -q -k profession_rejections
```

Expected: `DiscoveryStats` has no `profession_rejected` field.

- [ ] **Step 3: Implement separate profession-rejection counting**

Extend:

```python
@dataclass(slots=True)
class DiscoveryStats:
    raw: int = 0
    unique: int = 0
    prefilter_rejected: int = 0
    profession_rejected: int = 0
    eligible: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
```

In `collect_candidates()`:

```python
prefilter_result = prefilter_job(job, policy)
if not prefilter_result.should_evaluate:
    if prefilter_result.reason_code == "off_target_profession":
        stats.profession_rejected += 1
    else:
        stats.prefilter_rejected += 1
    continue
```

- [ ] **Step 4: Write failing pipeline budget tests**

In `tests/test_pipeline.py`, use fake jobs with unique `source_job_id` values and valid titles such as `Senior Software Engineer 001` through `Senior Software Engineer 090`. Configure the fake Gemini to count evaluation calls.

Add:

```python
def test_pipeline_evaluates_all_valid_jobs_below_default_ceiling(settings_75, store, fake_gemini):
    jobs = make_engineering_jobs(60)
    run_pipeline(settings_75, sources=[FakeSource(jobs)], store=store, gemini=fake_gemini, http=FakeHttp())
    assert fake_gemini.eval_calls == 60
```

and:

```python
def test_pipeline_caps_valid_jobs_at_75(settings_75, store, fake_gemini):
    jobs = make_engineering_jobs(90)
    run_pipeline(settings_75, sources=[FakeSource(jobs)], store=store, gemini=fake_gemini, http=FakeHttp())
    assert fake_gemini.eval_calls == 75
```

Ensure fake evaluations return a non-package `skip` result so these tests do not generate PDFs or Telegram traffic.

- [ ] **Step 5: Run budget tests and confirm failure**

```bash
pytest tests/test_pipeline.py -q -k "below_default_ceiling or caps_valid_jobs_at_75"
```

Expected: the first test evaluates only 25 under the current default/config fixtures.

- [ ] **Step 6: Add funnel logging and skipped accounting**

In `pipeline.py` change skipped accounting to:

```python
summary.skipped += discovery.stats.prefilter_rejected + discovery.stats.profession_rejected
```

After ranking:

```python
selected = ranked[: settings.policy.max_jobs_per_run]
deferred_by_budget = max(0, len(ranked) - len(selected))
```

Log:

```python
logger.info(
    "discovery: raw=%s unique=%s prefilter_rejected=%s profession_rejected=%s eligible=%s selected=%s deferred_by_budget=%s",
    discovery.stats.raw,
    discovery.stats.unique,
    discovery.stats.prefilter_rejected,
    discovery.stats.profession_rejected,
    discovery.stats.eligible,
    len(selected),
    deferred_by_budget,
)
```

- [ ] **Step 7: Add logging regression test**

With 90 valid jobs and limit 75, capture logs and assert one discovery log contains:

```text
selected=75 deferred_by_budget=15
```

Also add one Product Manager to the source and assert the same log contains:

```text
profession_rejected=1
```

- [ ] **Step 8: Run discovery/pipeline tests**

```bash
pytest tests/test_discovery.py tests/test_pipeline.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/job_hunter/discovery.py src/job_hunter/pipeline.py tests/test_discovery.py tests/test_pipeline.py
git commit -m "feat: expose profession gate and larger Gemini safety ceiling"
```

---

### Task 3: Filter Telegram decisions and sort by final Gemini score

**Files:**
- Modify: `src/job_hunter/telegram.py`
- Modify: `src/job_hunter/pipeline.py`
- Modify: `tests/test_telegram.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `select_deliverable_items(items: Sequence[DigestItem]) -> list[DigestItem]`
- Preserves: `build_digest(items: Sequence[DigestItem]) -> str`
- Preserves: `TelegramClient` public methods.

- [ ] **Step 1: Write failing Telegram filtering tests**

Add:

```python
def test_build_digest_omits_skip_decisions():
    digest = build_digest([
        _item(job_id=1, company="Keep", score=70, decision="possible_match"),
        _item(job_id=2, company="Drop", score=55, decision="skip"),
    ])
    assert "Keep" in digest
    assert "Drop" not in digest
```

and:

```python
def test_select_deliverable_items_omits_unknown_decision(caplog):
    items = select_deliverable_items([
        _item(job_id=1, company="Known", decision="possible_match"),
        _item(job_id=2, company="Unknown", decision="future_decision"),
    ])
    assert [item.company for item in items] == ["Known"]
    assert "future_decision" in caplog.text
    assert "job_id=2" in caplog.text
```

- [ ] **Step 2: Write failing score-order tests**

Add:

```python
def test_build_digest_sorts_each_section_by_score_descending():
    items = [
        _item(job_id=1, company="Low Ready", score=78, decision="package_match"),
        _item(job_id=2, company="High Ready", score=91, decision="high_priority"),
        _item(job_id=3, company="Low Possible", score=66, decision="possible_match"),
        _item(job_id=4, company="High Possible", score=73, decision="possible_match"),
    ]
    digest = build_digest(items)
    assert digest.index("High Ready") < digest.index("Low Ready")
    assert digest.index("High Possible") < digest.index("Low Possible")
```

Add a tie-breaker test with equal scores:

```python
def test_build_digest_uses_deterministic_tie_breakers():
    items = [
        _item(job_id=3, company="Beta", title="Engineer", score=80, decision="package_match"),
        _item(job_id=2, company="Acme", title="Z Engineer", score=80, decision="package_match"),
        _item(job_id=1, company="Acme", title="A Engineer", score=80, decision="package_match"),
    ]
    digest = build_digest(items)
    assert digest.index("Acme - A Engineer") < digest.index("Acme - Z Engineer")
    assert digest.index("Acme - Z Engineer") < digest.index("Beta - Engineer")
```

- [ ] **Step 3: Run Telegram tests and confirm failure**

```bash
pytest tests/test_telegram.py -q
```

Expected: `skip` falls into the blocker section and group items preserve input order.

- [ ] **Step 4: Implement explicit delivery selection**

In `telegram.py`, keep the existing section mapping but define the accepted decisions explicitly:

```python
_DELIVERABLE_DECISIONS = frozenset(_GROUP_HEADERS)


def select_deliverable_items(items: Sequence[DigestItem]) -> list[DigestItem]:
    selected: list[DigestItem] = []
    for item in items:
        if item.decision == "skip":
            continue
        if item.decision not in _DELIVERABLE_DECISIONS:
            logger.warning(
                "omitting unknown Telegram decision=%s job_id=%s",
                item.decision,
                item.job_id,
            )
            continue
        selected.append(item)
    return selected
```

Remove the fallback behavior that maps unknown decisions to `Needs review / blockers`.

- [ ] **Step 5: Implement deterministic section sorting**

Add:

```python
def _item_sort_key(item: DigestItem) -> tuple[int, str, str, int]:
    return (
        -item.score,
        (item.company or "").lower(),
        (item.title or "").lower(),
        item.job_id,
    )
```

In `build_digest()`, first call `select_deliverable_items(items)`, group only mapped decisions, and render each group with:

```python
for item in sorted(group_items, key=_item_sort_key):
```

If all input items are filtered out, return `No matching jobs today.`.

- [ ] **Step 6: Make pipeline mark only items actually sent**

Import `select_deliverable_items` in `pipeline.py`.

Before sending the digest:

```python
deliverable_items = select_deliverable_items(digest_items)
if deliverable_items:
    digest_text = build_digest(deliverable_items)
    message_id = telegram.send_message(digest_text)
    if message_id is not None:
        for item in deliverable_items:
            store.mark_delivered(item.job_id, "telegram_message", message_id)
```

Do not mark `skip` or unknown decisions as delivered.

- [ ] **Step 7: Sort PDF deliveries by final score**

Before the document-send loop:

```python
pdf_deliveries.sort(
    key=lambda entry: (
        -entry[2].score,
        (entry[2].company or "").lower(),
        (entry[2].title or "").lower(),
        entry[0],
    )
)
```

Add a pipeline test with two ready jobs evaluated in low-score-first order and assert `FakeTelegram.document_calls` records the higher-score job first.

- [ ] **Step 8: Add pipeline regression proving skip is not sent**

Create one fake job whose Gemini evaluation returns `decision="skip"` and score 50. Run with non-dry-run fake Telegram and assert:

```python
assert telegram.message_calls == []
assert telegram.document_calls == []
assert store.has_delivery(job_id, "telegram_message") is False
```

The evaluation must still exist in SQLite so the job is not unnecessarily re-evaluated next run.

- [ ] **Step 9: Run Telegram/pipeline tests**

```bash
pytest tests/test_telegram.py tests/test_pipeline.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/job_hunter/telegram.py src/job_hunter/pipeline.py tests/test_telegram.py tests/test_pipeline.py
git commit -m "fix: filter and score-sort Telegram results"
```

---

### Task 4: Preserve retry semantics and document v2.1 behavior

**Files:**
- Modify: `tests/test_store.py`
- Modify: `tests/test_pipeline.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Preserves: `JobStore.pending_delivery_job_ids() -> list[int]`
- Preserves: existing persisted-state schema.

- [ ] **Step 1: Add explicit store regression for skip decisions**

In `tests/test_store.py`, create a persisted job and `Evaluation` with `decision="skip"`. Assert:

```python
assert job_id not in store.pending_delivery_job_ids()
```

Also keep existing assertions proving `possible_match` without a message and ready decisions missing either message/document remain pending.

- [ ] **Step 2: Add retry regression for historical skip**

In `tests/test_pipeline.py`, persist a previously evaluated `skip` job with no delivery record, rediscover it on the next run, and assert:

```python
assert gemini.eval_calls == 0
assert telegram.message_calls == []
assert telegram.document_calls == []
```

This proves the new Telegram filter also protects the rediscovery/retry path.

- [ ] **Step 3: Run retry/store tests**

```bash
pytest tests/test_store.py tests/test_pipeline.py -q
```

Expected: PASS.

- [ ] **Step 4: Update README**

Document these exact user-visible rules:

```text
- Broad discovery is unchanged.
- Only software/product-engineering professions reach Gemini.
- The default Gemini safety ceiling is 75 valid jobs per run.
- `skip` evaluations are persisted but not sent to Telegram.
- Telegram sections are ordered by final Gemini score descending.
```

Document the three new YAML configuration lists and explain that blocked profession phrases take precedence over generic `engineer`/`developer` markers.

- [ ] **Step 5: Update AGENTS.md**

Update the architecture flow to include:

```text
all sources -> enrich/dedupe -> profession gate + prefilter -> deterministic rank -> top <=75 Gemini -> decision filter -> score-sorted Telegram
```

Document `PrefilterResult.reason_code`, `DiscoveryStats.profession_rejected`, and that Telegram delivery is fail-closed for unknown decisions.

- [ ] **Step 6: Run the full suite**

```bash
pytest -q
```

Expected: all tests PASS.

- [ ] **Step 7: Inspect configuration and secret safety**

```bash
git grep -n "max_jobs_per_run" config/search.yml src/job_hunter tests
git grep -n "CANDIDATE_PROFILE_B64=" -- ':!docs/superpowers/*' || true
git grep -n "COVER_LETTER_TEMPLATE_B64=" -- ':!docs/superpowers/*' || true
```

Expected: production config/defaults show 75; no secret values are committed.

- [ ] **Step 8: Commit**

```bash
git add tests/test_store.py tests/test_pipeline.py README.md AGENTS.md
git commit -m "docs: document v2.1 candidate quality rules"
```

---

## Final verification

- [ ] Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] Run a focused local dry run with configured secrets/profile:

```bash
JOB_HUNTER_DRY_RUN=1 python -m job_hunter run
```

Expected logs include `profession_rejected=` and `deferred_by_budget=`. No Telegram calls occur in dry-run mode.

- [ ] Inspect final scope:

```bash
git status --short
git log --oneline --max-count=10
git diff --stat main...HEAD
```

Expected: changes are limited to candidate gating, budget visibility, Telegram filtering/sorting, tests, and related docs/config.

- [ ] Push the implementation branch and confirm `.github/workflows/ci.yml` passes before merging.

## Implementation success check

The release is successful when off-target professions such as Product Manager, Product Designer, Sales Engineer, Data Engineer, ML Engineer, and mobile/embedded roles never consume Gemini calls; valid software/product-engineering jobs use a 75-job safety ceiling; `skip` and unknown decisions never reach Telegram; all delivered sections and PDFs are ordered by final Gemini score descending; and existing persisted state/retry tests remain green.

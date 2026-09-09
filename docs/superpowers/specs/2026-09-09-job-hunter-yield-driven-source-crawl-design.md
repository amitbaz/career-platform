# Yield-driven per-source crawling (issue #184)

Status: approved design, not yet implemented.
Branch: `feat/job-hunter-yield-driven-source-crawl`, based on `ab35e3f`.
Parent epic: #181. Blocked behind #179.

## What this changes

Crawling stops being a step inside one daily per-user run and becomes one
message per source on the `crawl_source` queue, each source enqueued on its own
cadence derived from how much genuinely new material it produces. A source that
is rate-limited or failing stalls only itself. An unchanged board costs a
conditional request and nothing else.

Three things the ticket needs but does not name, and which are therefore part of
this design: sources become an entity with a durable identity, the external
search budget becomes a platform ledger, and #183's scheduling helper is fixed
so that per-source schedules do not collapse into one.

## Why this is one pull request

The owner's decision, taken after being shown the alternative. This work is
five separable subsystems (source registry, search-ledger move, `crawl_source`
stage handler, incremental crawl, yield scheduler), and the recommendation was
five sub-issues landing in dependency order. The owner chose a single pull
request closing #184 in one merge. The design below is therefore written as one
migration and one branch, with the internal ordering preserved as commit
boundaries rather than as pull-request boundaries.

## Decisions taken, with their reasons

### The schedule follows corpus novelty, not per-user eligibility

The `crawl_source` scheduler runs as the privileged owning role with no
`auth.uid()`. "Measured yield" split in two when #203 landed: board health
became shared knowledge on `job_hunter_ats_boards`, while `eligible_jobs_seen`
and `last_eligible_at` stayed per-user on `job_hunter_ats_registry`. That
migration's own comment says "#184 (yield-driven per-source crawl) is built on
it" — and that line is now stale, for two reasons.

First, mechanically: an identity-free scheduler cannot read a per-user column,
and threading a user through to reach it is the mistake #174 and #175 spent four
tickets undoing.

Second, and worse: with one user today, an aggregate of per-user eligibility
*is* that one user's search profile. The engine would learn to visit only the
boards matching the owner's current job hunt, and would then present a narrowing
corpus as a quiet job market. #203 already warns that a shared row carrying one
user's yield misleads budgeting for the others; the same argument applied to
scheduling gives the same answer.

So this ticket records a new, genuinely shared signal: per-source corpus
novelty. On every crawl, identity-free: how many listings were fetched, how many
were new to the corpus, how many changed, how many were dropped as unchanged by
description hash, what it cost. That is the same measure for every user, it
scales with jobs rather than with subscribers, and it is what the scheduler can
actually see.

Action: correct the stale comment in
`20260909200000_job_hunter_ats_boards.sql` as part of this change, so the next
reader is not sent down the per-user path.

### The display obligation is called `display_credit`

`attribution` already means market attribution throughout the ingestion code —
`_cheap_market_attribution`, `_record_reattribution`, `stats.reattributed_*`,
roughly twenty sites in `discovery.py` alone, plus tests named for it. A second
meaning on the same word would be read wrong by everyone including us.

`display_credit` is the licensing term of art and covers both halves of the
obligation: the required text credit and the required badge.

### The search budget becomes a platform ledger

The owner's decision, recorded on #184. The quota is a property of the API key,
not of a person. A per-user ledger has two possible behaviours and both are
wrong: charge one arbitrary user for everyone's crawl, or fan the crawl out per
user and burn the same cap N times for identical results.

The existing per-user rows are collapsed onto the provider and carried across.
The Brave monthly cap is drawn against the key, so calls already spent this
month are real spend regardless of whose row recorded them; starting the
platform ledger empty would hand the engine a fresh allowance on a key that has
already been drawn down.

### The cursor is the HTTP validator for most sources

Acceptance criterion 1 asks for a cursor that advances so a crawl resumes rather
than starting over. For most of the current portfolio — `remotive`, `arbeitnow`,
`jobicy`, `himalayas`, `remoteok`, `weworkremotely`, `lever`, `greenhouse`,
`ashby` — there is no pagination cursor to resume from: the whole board arrives
in a single `GET`. For those sources the honest cursor is the HTTP validator,
`ETag` or `Last-Modified`, which is what makes the *next* fetch conditional and
therefore cheap. A high-water timestamp is stored alongside it for the sources
that accept a `since`-style parameter.

This is recorded rather than papered over: inventing a synthetic cursor for a
source that has none would satisfy the checkbox and change nothing about the
cost.

### Acceptance criterion 6 measures itself

The 14,014-postings figure came from run 34289288702, which was cancelled at the
timeout. It is a partial count of unknown completeness, so a delta against it
can be argued either way on the same evidence.

Instead, `job_hunter_source_crawls` records `fetched` and `new_to_corpus` on
every crawl, so the first post-merge crawl reports its own ratio directly. That
is stronger evidence than a comparison against a partial count, and it keeps the
charter's rule that claims about the engine are measured rather than asserted.

## Schema

One migration file. Its timestamp is allocated at pull-request open, in merge
order, per the policy set by PR #202; the working name is
`PLACEHOLDER_job_hunter_source_registry.sql` — deliberately not a timestamp, so
it cannot be mistaken for an allocated one.

Job Hunter has two established shapes for a table with no `user_id`, and this
migration uses both. Shared *knowledge* — what an advertisement says to
everybody — takes #179's shape: `select` open to `authenticated`, writes revoked
from every role a user can hold, and a pgTAP test proving the refusal. Shared
*machinery* — operational state no user reads — takes the platform-ledger shape:
row-level security on with no policy, and grants revoked as well, so neither
half is load-bearing alone.

### `job_hunter_sources` — shared knowledge

```
id             uuid primary key
source_key     text not null unique      -- matches JobSource.source_label
kind           text not null check (kind in ('crawl', 'licensed'))
display_credit jsonb not null default '{}'::jsonb
enabled        boolean not null default true
first_seen_at  timestamptz not null default now()
created_at     timestamptz not null default now()
```

`source_key` matches the existing `JobSource.source_label`, so `remotive` and
`lever:acme` stay apart exactly as they already do in the discovery cost
statistics. No new naming scheme is introduced.

`kind` exists because of the owner's first constraint: a licensed API is a
first-class source kind, not a variant of a crawl. It has its own rate
accounting — a contractual quota of calls, not a politeness delay — its own
cadence, and its own display obligations. Carrying `kind` from the start is what
stops "source" from silently meaning "crawl". No licensed source is enabled by
this ticket; the shape is present, the portfolio stays scraped.

`display_credit` is the owner's second constraint. It is written as the general
rule, not as an Adzuna special case:

```json
{
  "required": true,
  "text": "Jobs by Adzuna",
  "link_text": "Jobs",
  "link_url": "https://www.adzuna.co.uk/",
  "badge_url": "https://…/adzuna-logo.png",
  "badge_min_px": [116, 23]
}
```

A resolver, `job_hunter_posting_display_credit(p_posting_id uuid)`, joins
`job_hunter_postings.source` to `job_hunter_sources.source_key`. There is no
`user_id` anywhere in that path, because the obligation is a property of the
posting's source and never of the reader. #188 reads it: a surface that cannot
render `badge_url` — Telegram's `sendMessage` has no `img` entity — contributes
`text` and `link_url` and never the advert body.

### `job_hunter_source_crawls` — shared machinery

```
id                uuid primary key
source_key        text not null
started_at        timestamptz not null
finished_at       timestamptz
outcome           text not null check (outcome in
                    ('fetched', 'not_modified', 'rate_limited', 'failed'))
fetched           integer not null default 0
new_to_corpus     integer not null default 0
changed           integer not null default 0
unchanged_by_hash integer not null default 0
requests          integer not null default 0
elapsed_ms        integer not null default 0
error             text not null default ''
```

One row per crawl attempt, written unconditionally — including, and especially,
the attempt that produced nothing. `outcome` is what makes an empty result carry
its reason: `not_modified` is a healthy unchanged board, `rate_limited` and
`failed` are not, and none of the three is indistinguishable from "nothing new
today". Today that distinction exists only as a log line from
`pipeline._log_source_metrics`; the ticket's claim that these metrics are
"already recorded" is true only of the log, and nothing persists them.

Index on `(source_key, started_at desc)` — the scheduler's only read.

### `job_hunter_source_cursors` — shared machinery

```
source_key    text primary key
etag          text not null default ''
last_modified text not null default ''
high_water_at timestamptz
updated_at    timestamptz not null default now()
```

Held apart from `job_hunter_sources` because the two take different policy
shapes: the registry is knowledge a user may read, the cursor is hot machinery
nobody reads. Folding them would force one shape onto both.

### `job_hunter_platform_search_usage` — platform ledger

Replaces `job_hunter_search_api_usage`. Keyed `(provider, occurred_at)`, copying
`job_hunter_platform_ai_usage`'s shape exactly: `runner_select`,
`runner_insert`, `runner_update` on the `job_hunter_runner` JWT claim, and no
delete policy — a ledger that can be rewritten is not a ledger. That shape is
reachable both over PostgREST, so `search_budget.py` changes little, and from
the privileged connection the `crawl_source` stage holds.

Backfill collapses the existing rows onto the provider, preserving
`occurred_at`, so this month's spend carries across.

Moving with the table: the unique constraint from
`202609060003_job_hunter_write_idempotency.sql`, the window index,
`search_budget.py`, `test_brave_budget.py`, and `conftest.py`'s cleanup list. It
comes *out* of the enforced private-data inventory in
`supabase/tests/pgtap/job_hunter_isolation.sql` and out of
`job_hunter_write_idempotency.sql`, and the new table's own refusal test is
added in their place.

### Fixing #183's collapsing schedule

`job_hunter_schedule_stage_enqueue` builds the cron job name from the stage
alone:

```sql
cron.schedule('job-hunter-enqueue-' || replace(p_stage, '_', '-'), …)
```

`cron.schedule` replaces by name. Installing N per-source schedules under one
stage-derived name means each silently replaces the last, and exactly one source
is ever visited. It presents as a quiet job market rather than as an error, so
nothing reports it. `supabase/tests/pgtap/job_hunter_stage_queues.sql` asserts
that literal name and therefore currently locks the defect in.

The fix: add `p_schedule_key text default null`. When given, the job name
becomes `job-hunter-enqueue-<stage>-<slug(key)>`. A helper,
`job_hunter_source_schedule_slug(text)`, lowercases, replaces every non
alphanumeric run with a hyphen, and appends a short hash when the result would
exceed the identifier limit, so `lever:acme` becomes `lever-acme` and two
distinct keys can never collide.

The existing assertions are rewritten, and a new one is added proving that two
different keys produce **two** rows in `cron.job`. The absence of that assertion
is what let this hide.

### `job_hunter_reschedule_sources()`

The scheduler. `security invoker`, `execute` revoked from `public`, `anon`,
`authenticated` and `service_role`, matching #183's two functions; it runs on
ingestion's privileged connection, which must be the owning role.

It reads `job_hunter_source_crawls`, bands each enabled source on recent
novelty (`new_to_corpus + changed` over its last several crawls), and installs
one cron entry per source through the fixed helper, unscheduling sources that
are disabled or gone.

Bands: 15 minutes, 1 hour, 6 hours, 24 hours, 72 hours, 7 days. Every crawl
returning no novelty demotes one band; any novelty promotes one; `rate_limited`
and `failed` back that source's own entry off without touching any other. A
source producing nothing for a week converges on the 7-day floor by itself, and
recovers a band at a time when it starts producing again.

No operator sets these. `Settings` may pin one source's band as an override, and
that override is never the mechanism — the bands are derived from data with zero
operator knowledge, per the charter.

The scheduler verifies its own effect against `job_hunter_stage_queue_metrics()`
rather than inventing a second notion of queue depth, per the owner's third
comment on #184.

## Python

### Conditional requests belong in `http.py`

Every one of the eighteen adapters funnels through `HttpClient.get_json()`,
which today calls `raise_for_status()` and would therefore raise on a `304`.
Adding a validator-aware path at that single site gives all eighteen sources
conditional requests without touching any of them.

`get_json` gains an optional validator pair and returns a `NotModified`
sentinel on `304` instead of raising. The `crawl_source` handler reads the
source's cursor before the crawl and writes the response's `ETag` and
`Last-Modified` after it. A `304` records `outcome='not_modified'` and stops
there, so an unchanged board never presents as an empty one.

### `crawl_source.py`

The new stage handler, mirroring `resolve_persist.py` in shape and in what it
refuses to import: no user-scoped store, no matching, no scoring, no
credentials. Payload is `{"source_key": "…"}` and nothing else, validated the
way `ResolvePersistStage._batch_id` validates its own.

It builds that one source, drains it, computes each listing's description hash
**client-side**, reads the shared `fingerprint → description_hash` set for the
fingerprints it saw, drops the ones whose hash is unchanged before staging, and
stages only the remainder. That is acceptance criterion 3: the hash is computed
inside `job_hunter_upsert_posting` today, which is far too late to prevent the
work it is meant to prevent. It then enqueues `resolve_persist` for the batch
and writes the `job_hunter_source_crawls` row.

### Other call sites

- `sources/base.py`: `JobSource` gains `source_key` and an optional cursor
  handshake, both with defaults, so the Protocol stays backward-compatible and
  `test_sources_incremental.py` continues to hold.
- `sources/__init__.py`: `build_sources` gains a single-source form so the stage
  handler can construct one source without building the whole portfolio.
- `pipeline.py`: `_log_source_metrics` keeps its log line and gains a persisted
  counterpart.
- `search_budget.py`: platform ledger, `user_id` gone from `record`, `count` and
  `try_record`.

## Testing

`pnpm job-hunter:test` with the `SUPABASE_TEST_*` variables exported. Without
them the suite is green with hundreds of skips and a migration is verified by
nothing.

New coverage:

- pgTAP: two schedule keys produce two `cron.job` rows — the assertion whose
  absence hid the collapse.
- pgTAP: `job_hunter_sources` refuses writes from `anon`, `authenticated` and
  `service_role`, and permits `select` to `authenticated` (#179's shape).
- pgTAP: `job_hunter_source_crawls` and `job_hunter_source_cursors` refuse
  everything to every non-owning role (#183's shape).
- pgTAP: `job_hunter_platform_search_usage` enforces its unique key, and the
  runner-claim policies hold.
- pgTAP: `job_hunter_posting_display_credit` resolves for a posting whose source
  declares an obligation, and returns nothing for one that does not — called
  with no user in the path.
- Python: a `304` records `not_modified` and stages nothing.
- Python: a listing whose description hash is unchanged never reaches staging.
- Python: a source raising a rate-limit error records `rate_limited` and leaves
  every other source's cadence untouched.
- Python: the band function demotes on an empty crawl, promotes on novelty, and
  converges on the floor, with no configuration supplied.

## Ordering and risk

**#179 must land first.** It is in flight on `feat/job-hunter-shared-table-writers`,
its migration `20260909220000` is already hand-applied to the shared local
stack, and it edits the same enforced-isolation inventory this change edits. It
also revokes writes on `job_hunter_ats_boards`, which this change reads. This
branch's migration does not open until #179 merges.

Risks worth stating:

- The signature change to `job_hunter_schedule_stage_enqueue` touches a function
  that is already merged. Any other branch calling the three-argument form
  breaks. Broadcast on the coordination log; no caller found on `main` today,
  because #183 deliberately installed no schedule.
- The search-ledger move touches four files outside `apps/job-hunter/src` and
  the enforced private-data inventory. If #179 changes that file first, this
  rebases onto it rather than the reverse.
- Eighteen adapters gain conditional requests through one shared code path. If
  that path is wrong, it is wrong everywhere at once — which is the argument for
  the `304` test being a unit test at the `HttpClient` level and not only an
  integration test through one source.

## Out of scope

- Enabling any licensed source. The shape is built; the portfolio stays
  scraped. Enabling one is gated on a web surface that can display it legally,
  which does not exist and is its own ticket.
- The web surface itself.
- Per-user search accounting. It returns if and when a user-triggered search
  exists, and not before.
- #186's re-check scheduling, which inherits this ticket's shape but is not
  changed here.

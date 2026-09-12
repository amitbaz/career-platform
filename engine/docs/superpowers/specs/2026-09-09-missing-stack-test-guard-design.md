# Missing Stack Test Guard Design

## Problem

The Job Hunter suite currently treats a missing local Supabase environment as a reason to skip
every store-backed test. Pytest then exits successfully, so an accidental unit-only run looks
like a successful full run.

## Decision

Fail once at pytest session start when the required local-stack environment is incomplete.
The failure names every missing variable and explains both explicit opt-outs:

- `JOB_HUNTER_ALLOW_MISSING_STACK=1` for callers that cannot add pytest arguments.
- `--allow-missing-stack` as command-line sugar.

The opt-out is honored only when none of the three required variables are set. A partial
configuration always fails, even when either opt-out is present, because it indicates a typo or
broken environment propagation rather than an intentional non-database run.

The required variables are:

- `SUPABASE_TEST_URL`
- `SUPABASE_TEST_PUBLISHABLE_KEY`
- `SUPABASE_TEST_SIGNING_KEY_B64`

`SUPABASE_TEST_DB_URL` remains outside this guard. It is an optional direct Postgres connection,
not part of the PostgREST stack environment shared by every store-backed test.

## Behavior

| Required variables | Opt-out | Result |
| --- | --- | --- |
| All set | Either value | Run normally; store-backed tests do not skip for missing stack configuration |
| None set | Absent | Abort once at session start with a usage error |
| None set | Environment variable or flag | Run non-database tests; `_stack_env` skips store-backed tests with an explicit reason |
| Some set | Any value | Abort once at session start and name the missing variables |

The environment opt-out accepts exactly `1`. This keeps an accidental or misspelled value from
silently disabling the store suite.

## Implementation

`tests/conftest.py` owns the policy because it already owns the required-variable list and the
fixture used by every store-backed test. `pytest_addoption` registers the flag. A small policy
helper classifies the environment as complete, absent, or partial, and `pytest_sessionstart`
raises `pytest.UsageError` before collection for every invalid state. `_stack_env` retains its
skip only for the explicit, fully absent opt-out path.

Harness tests run a nested pytest session with `pytester`. They assert observable behavior:

- absent configuration exits with a usage error that appears once and names all variables;
- both opt-outs permit a deliberate non-database run and skip the probe store-backed test;
- partial configuration fails under both opt-outs and names only the missing variables;
- complete configuration runs the probe store-backed test instead of skipping it.

The tests do not assert a global zero-skip count. Other legitimate skips vary by branch; the
claim enforced here is specifically that missing core stack configuration cannot silently skip
store-backed tests.

## Documentation

Extend the existing root `AGENTS.md` section introduced by PR #206 and amended by PRs #211 and
#216. Replace the manual zero-skip invariant with the enforced default failure and the deliberate
opt-out commands. Keep the surrounding shared-stack and migration-ledger guidance in place.

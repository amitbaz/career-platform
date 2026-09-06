# Recovering an ATS board that aggregator detection rejected wrongly

Issue: #63 — Give a wrongly rejected ATS board a recovery path.

## Problem

Aggregator detection (#17) rejects a learned ATS board permanently, and nothing
in the codebase can reverse that verdict.

`JobStore.reject_ats_board` sets `active = 0` and writes `rejected_reason`.
From that point:

- `list_due_ats_boards` filters on `active = 1`, so the board is never scanned
  again, and detection can therefore never revise its own verdict — revising it
  would require the very scan the rejection prevents.
- `upsert_ats_board` deliberately refuses to reactivate a board while
  `rejected_reason` is set, so ordinary rediscovery cannot bring it back.
- No code path anywhere sets `rejected_reason` back to `NULL`. The column is
  written by `reject_ats_board`, read by `list_rejected_ats_boards` and
  `_ats_entry_from_row`, and never cleared.
- Removing the board from `learned_ats_denylist` does not help: the denylist
  only ever adds rejections and has no effect on a `rejected_reason` already
  persisted.

Recovery today means hand-editing `var/job_hunter.sqlite3`, which in the
deployed setup lives inside a GitHub Actions artifact — download the artifact,
edit the database, restore it.

The detection signal itself is sound: a board is rejected when a majority of
its scanned postings declare the role belongs to another company. But that
phrasing is not unique to aggregators. A consultancy, a staffing-adjacent
employer, or a company running a contract-hiring wave can genuinely publish
postings that read "this role is with our client". The cost of being wrong is
asymmetric: an aggregator that slips through wastes one scan, while a wrongly
rejected employer disappears from discovery permanently.

## Approach

Add `learned_ats_allowlist` to `config/search.yml` — the inverse of the
existing denylist, holding `"<provider>:<board>"` keys that detection may never
reject. Config is already the operator surface for the denylist, so the
operator-facing model stays symmetric: one list forces rejection, one prevents
it, both diffable and reviewable in git, neither needing a new CLI surface.

### Semantics: "may never be rejected", not "always admitted"

The allowlist governs verdicts about a board's *nature* only — aggregator
detection and the denylist. It deliberately does not touch scan-failure health:
`record_ats_scan_failure`, `paused_until` backoff, and stale-404 deactivation
stay in force for an allowlisted board. A board that 404s is broken, not
misjudged, and conflating the two would let an allowlist entry keep a dead
board in the due rotation forever.

The allowlist also does not force admission of a board that was never
registered. `harvest_ats_board` already admits by default; a board only fails
admission when it is denylisted, and the deny/allow conflict rule below makes
that combination impossible.

### Configuration

`learned_ats_allowlist` mirrors `learned_ats_denylist` exactly, parsed by the
same rules:

- a list of `"<provider>:<board>"` strings, each normalized through
  `ats_board_key` so every consumer compares like for like;
- a bare `learned_ats_allowlist:` key (the state left behind by commenting out
  its only entry) reads as an empty list rather than aborting the run;
- a malformed entry raises `ValueError` naming `learned_ats_allowlist[<index>]`.

It surfaces as `PolicySettings.learned_ats_allowlist: list[str]`.

Because both parsers normalize to the same key form, the two lists can be
compared directly once both have parsed. A key present in both raises
`ValueError` naming the key. The two lists express opposite operator intent, so
there is no correct way to honour both; silently preferring either one would
hide an editing mistake in the file that is the whole operator surface, and
would hide it in exactly the direction that matters least to the person who
made it. Failing the load is consistent with how every other malformed policy
value behaves.

### Enforcement

Three points, all in `LearnedAtsSource`.

**1. Healing, at the start of `discover()`.** For every allowlisted key that
currently carries a `rejected_reason`, clear the rejection and reactivate the
board, then read the due boards. A board recovered this way is scanned in the
same run that recovered it, so editing `config/search.yml` is the entire
recovery procedure — no database surgery, no artifact download, no second run.

This needs one new store method, `clear_ats_board_rejection(provider,
board_identifier)`: the inverse of `reject_ats_board`, setting
`rejected_reason = NULL` and `active = 1`. It is the first code path in the
project that clears the column.

Healing lives in `LearnedAtsSource` rather than the pipeline because that class
already owns the denylist-to-rejection direction; putting its inverse anywhere
else would split two halves of one operator contract across two files.

Each healed board is logged individually, naming the reason that was cleared —
the reason is the only record of what the operator overrode, and it is
destroyed by the healing write.

**2. The denylist branch.** An allowlisted key is never rejected there. With
the conflict check in place this branch is unreachable for an allowlisted
board, but the guard is cheap and keeps the invariant local to the code that
depends on it rather than resting on a validation two modules away.

**3. `_aggregator_rejection`.** Detection still runs. When `evaluate_board`
returns a rejecting verdict for an allowlisted board, the board is kept and the
overridden verdict is logged:

```
learned ATS board kept by learned_ats_allowlist despite <reason>
```

Running detection and discarding its verdict, rather than skipping detection
for allowlisted boards, is deliberate. The operator overrode a verdict, so the
verdict they overrode must stay visible in the run log. Otherwise the allowlist
entry becomes unfalsifiable: nobody can see whether the detector still
disagrees, whether the board's postings changed, or whether a later improvement
to detection made the entry unnecessary. The cost is one detection pass over
postings already fetched — no extra requests, no extra scan.

### What does not change

`upsert_ats_board` keeps its guard unconditionally. Rediscovery still cannot
resurrect a rejected board; only an explicit allowlist entry can. Detection's
signal, its threshold, and its minimum sample size are untouched — this is
about recovering from a verdict, not about improving the verdict.

## Data flow

```
config/search.yml
  learned_ats_denylist  ─┐
  learned_ats_allowlist ─┴─> config.load_settings (conflict check)
                                    │
                                    v
                          PolicySettings.learned_ats_allowlist
                                    │
                                    v
                          LearnedAtsSource(allowlist=...)
                                    │
        ┌───────────────────────────┼──────────────────────────┐
        v                           v                          v
  heal rejected            skip denylist            keep despite
  allowlisted boards       rejection                aggregator verdict
  (clear_ats_board_        for allowlisted          (log the overridden
   rejection)              boards                    reason)
        │
        v
  board is due again, scanned this run
```

## Failure behaviour

- A conflicting deny/allow entry fails the config load, which fails the run
  before any discovery work. This is the same blast radius as any other
  malformed policy value.
- A healing write that raises is logged as a warning and does not abort the
  run, matching how `_reject_board` and the health writes already isolate
  per-board store failures. The board stays rejected and the next run retries
  the healing.
- Detection failing open is unchanged: an unexpected exception in
  `evaluate_board` keeps the board regardless of the allowlist.

## Testing

Red-green-refactor throughout; every point below is a failing test first.

`tests/test_config.py`
- allowlist absent parses to `[]`;
- entries are normalized (`" Lever:JobGether "` → `lever:jobgether`);
- bare `learned_ats_allowlist:` key parses to `[]`;
- a malformed entry raises, naming `learned_ats_allowlist`;
- a key in both lists raises, naming the key.

`tests/test_store.py`
- `clear_ats_board_rejection` NULLs `rejected_reason` and sets `active = 1`;
- the cleared board leaves `list_rejected_ats_boards` and reappears in
  `list_due_ats_boards`;
- clearing a board that was never rejected is a no-op, not an error.

`tests/test_sources.py`
- an allowlisted board whose postings trigger detection is kept, counted in
  `boards_successful`, and its jobs are returned;
- an allowlisted board that also appears in the denylist cannot occur (covered
  by the config test), but an allowlisted board is not rejected by the denylist
  branch when constructed directly with both sets;
- an already-rejected allowlisted board is healed and scanned within the same
  `discover()` call;
- regression on #17: a non-allowlisted board with the same postings is still
  rejected.

Then the full `pytest -q` suite before opening the PR.

## Documentation

- `config/search.yml`: comment the new key beside the denylist.
- `aggregator_detection.py` module docstring: it currently describes
  `learned_ats_denylist` as the only override and states the mechanism needs no
  operator configuration. Both statements need the allowlist added without
  weakening the first claim — the allowlist is still an override, not the
  mechanism.
- Job Hunter README / AGENTS.md wherever policy keys are enumerated.

## Non-goals

- Changing the detection signal or its threshold.
- Re-litigating whether boards should be rejected at all (#17 settled that).
- Any automatic un-rejection: recovery requires an explicit operator edit.
- A CLI subcommand. On its own it buys one run before the board is rejected
  again, and with the allowlist in place it adds a second operator surface for
  the same decision.

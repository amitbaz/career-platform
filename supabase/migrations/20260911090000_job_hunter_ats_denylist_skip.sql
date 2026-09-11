-- Distinguish never-checked-because-new from never-checked-because-excluded
-- (issue #226).
--
-- `learned_ats.py::discover()` filters a config-denylisted board out of
-- `remaining` before `list_due_ats_boards`'s entries ever reach
-- `select_ats_boards`, so `job_hunter_ats_boards.last_checked_at` is never
-- set for it -- there is no legitimate scan to attribute that timestamp to.
-- Without a way to record the skip, the board sits in the never-checked
-- ranking tier every run, forever, and can outrank a genuinely new board on
-- the `board_identifier` tie-break the moment it sorts first.
--
-- The fix cannot be a write to `job_hunter_ats_boards.rejected_reason` (or
-- any other column on that table): #203 already separated aggregator
-- evidence about a board (shared, generalizes across users) from a config
-- denylist (one user's own policy) precisely so a denylist entry could never
-- become binding on anyone else. A denylist skip belongs on the per-user
-- `job_hunter_ats_registry` row instead, next to `last_eligible_at` -- the
-- other per-user signal `select_ats_boards`' ranking already reads.
alter table public.job_hunter_ats_registry
  add column denylist_skipped_at timestamptz;

comment on column public.job_hunter_ats_registry.denylist_skipped_at is
  'Set by record_ats_board_denylist_skip when this user''s own '
  'learned_ats_denylist excludes the board, so it leaves the never-checked '
  'ranking tier for this user without ever touching the shared '
  'job_hunter_ats_boards row or its rejected_reason (issue #226). RLS keeps '
  'it invisible to, and non-binding on, every other user.';

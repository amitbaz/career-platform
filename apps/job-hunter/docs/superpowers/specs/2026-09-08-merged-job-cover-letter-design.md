# Merged-Job Cover-Letter Recovery Design

## Goal

Keep an old Telegram card's **Gen CL** action working after its job has been
merged into a surviving job, and give the user a clear response when the job is
truly gone.

## Context

Issue #146 identifies the shared behavioral seam:
`generate_cover_letter_on_demand`. Both the Telegram webhook dispatch and the
`generate-cover-letter --job-id` CLI command call this function. A discovery
merge deletes the duplicate job row, while `job_hunter_job_merges` and
`PostgresJobStore.resolve_merged_job_id` retain the surviving job id.

## Design

- Keep the card's job id as the initial lookup key so the common live-job path
  remains unchanged.
- When that job is absent, ask `PostgresJobStore` for its merge redirect. If a
  redirect exists, fetch the surviving job and use its id for the evaluation,
  saved material, and Telegram document delivery record.
- Resolve before checking saved material so the existing free resend behavior
  applies to the survivor without calling Gemini.
- When neither a live job nor a redirect exists, send a short generic Telegram
  message that the job is no longer available and return `False` without
  calling Gemini.
- Keep document text, credentials, and other private material out of new log
  and user-message lines.

## Testing Seam

Test through `generate_cover_letter_on_demand` with the existing Postgres store,
fake Gemini client, and fake Telegram client. This is the highest shared seam
for both entry points and observes generation, resend, notification, persisted
material, and delivery behavior without testing internal helpers.

## Out of Scope

- Rewriting stored Telegram navigation sessions or cards.
- Changing merge persistence or redirect semantics.
- Adding redirect handling to unrelated surfaces.

## Success Criteria

1. A merged-away job id generates and delivers against the surviving job.
2. Material and delivery records use the surviving job id.
3. Existing survivor material is resent without a Gemini call.
4. An id with no job and no redirect sends an informative message and makes no
   model call.
5. The Job Hunter test suite passes.

# ADR-0001: The search-and-match engine is the product

Status: accepted
Date: 2026-09-08

## Context

This system began as a Telegram bot that ran once a day and sent one person a list of jobs. Everything about its shape follows from that origin: discovery, filtering, evaluation and delivery happen in a single process, in a single pass, and the only way to get anything out of it is to wait for the scheduled run.

That shape has three consequences, and they turned out to be the same problem seen from different angles.

Cost scales with users rather than with jobs, because a posting is read and judged separately for every person it might interest. Nothing can ask the system a question, because there is no corpus to query — only a batch that already decided. And a single slow source can consume the entire run, because the crawl sits on the critical path of the only output there is.

The product intent that emerged is broader than a bot: a browsable corpus with filters, an on-demand search, a daily curated digest, interview preparation, application tracking, and prepared answers to recurring application questions. Several of those cannot sit on a once-a-day crawl-then-match batch at all.

## Decision

**The search-and-match engine is the product. Everything else is a surface.**

The engine is three layers with independent schedules and independent failure domains:

- **Ingestion** — sources into normalized job rows. Shared across all users, because job postings are public data and crawling the same board once per user is waste with no isolation argument behind it.
- **Enrichment** — objective facts about a posting, extracted once per job and cached: hiring-eligible regions, remote policy, seniority, compensation, stated requirements and their depth. Shared and platform-funded, because these are properties of the job and identical for everyone.
- **Matching** — a user's profile and filters scored against enriched rows. Per-user, funded by that user, and fast enough to answer on demand.

Telegram is a surface. The daily scheduled run is a surface. Any future application is a surface. None of them is the place where matching logic lives.

## Consequences

**Match quality is the product.** Everything else is packaging. When a change could improve match quality or improve a surface, match quality wins.

**Cost must scale with jobs, not with users.** Any change that makes enrichment per-user rather than per-job is a regression, however convenient. Two users interested in the same posting must not pay to read it twice.

**No matching, ranking, eligibility or scoring logic may live in a surface.** A surface asks the engine and renders the answer. Logic that leaks into an adapter has to be extracted again before the next surface can exist.

**Claims about match quality must be measured, not asserted.** The system already records yield per source; response rate by match-score band is what makes the score falsifiable. Until a claim has a number behind it, treat it as a hypothesis.

**Surfaces are deferred relative to the engine.** Work that only pays off if the product finds an audience — onboarding, a dashboard, a native application — is sequenced behind work that pays off regardless. The engine is useful to its single current user on its own.

## Alternatives considered

**Keep the single-pass pipeline and optimise it.** Rejected. It cannot serve an on-demand query at any speed, so it forecloses the dashboard and manual search entirely, and it leaves per-user cost proportional to user count.

**Split ingestion out but keep enrichment per-user.** Rejected. It preserves the bring-your-own-key model in its purest form, but every user then pays to re-derive identical facts about the same posting, which is both the largest cost and the largest source of latency. The split into shared-objective and per-user-subjective enrichment keeps the guarantee where it matters — judging a person's fit stays funded by that person — while paying for shared facts once.

## References

- Epic #114 holds the locked decisions in full, including capacity analysis and the runtime choice.
- The multi-user epic #34 predates this decision; #73 and #76 have been revised to match it.

# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ----------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

`needs-rewrite` is this repo's addition: the issue's direction changed, and it must be
re-specified against `docs/product-vision.md` before anyone works on it.

`needs-owner` is an agent loop stopped and needs the owner's decision; the latest `@amitbaz`
comment says which one. Removed by the owner once they have answered.

## Kind labels

What kind of work an issue is, alongside its area. Optional; add one when it applies.

| Label         | Meaning                                                                          |
| ------------- | -------------------------------------------------------------------------------- |
| `bug`         | Something is broken                                                              |
| `enhancement` | A new capability or improvement                                                  |
| `research`    | Explore or decide before building; the output is a decision or a spec, not code |

A `research` issue is done when its decision is recorded (in `docs/product-vision.md`, an ADR
or a spec) and the implementation tickets it produced are filed, not when code ships.

## Area labels

Separate from triage state, every open issue carries exactly one area label (epics are
exempt). `.github/workflows/issue-areas.yml` checks this daily.

| Label             | What belongs there                                                                  |
| ----------------- | ----------------------------------------------------------------------------------- |
| `area:ingestion`  | Sources, crawling, freshness, and enrichment of the shared corpus; the stages       |
| `area:matching`   | Ranking, the why line, stack selection, learning from swipes and outcomes           |
| `area:applying`   | Prepared applications, the bucket, submitting (D2, D8)                              |
| `area:outcomes`   | What happened after applying: inbox, forwarding, check-ins, response rates (D3, D9) |
| `area:coach`      | Interview preparation and the user's story set (D5)                                 |
| `area:app`        | The mobile app and its desktop helper                                               |
| `area:platform`   | Infrastructure, QA, security, database and repository hygiene                       |
| `area:business`   | Launch, pricing, legal and commercial posture                                       |

When an issue straddles two areas, label it with the area whose code or decision it changes
first, and link the other area's issue if one exists. If no area fits, ask the owner rather
than inventing one.

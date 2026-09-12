#!/usr/bin/env python3
"""Guard that `engine/AGENTS.md` names exactly the shared tables.

    python3 scripts/check_job_hunter_shared_tables_doc.py

`supabase/tests/pgtap/job_hunter_isolation.sql` maintains the enforced list
of Job Hunter tables with no `user_id` -- `pg_temp.job_hunter_shared_tables`
-- and fails a test whenever that list stops matching the grants pgTAP
observes. `engine/AGENTS.md` restates that same set in prose, for
whoever reads the doc before touching a migration, but nothing tied the two
together (#215): #198 added `job_hunter_companies` to the pgTAP list and to
`CONTEXT.md`, not to this prose, and git produced no conflict because the
two files disagree in words rather than in text.

So the prose carries one more line: a `job-hunter-shared-tables` HTML
comment, invisible when the doc renders, listing the same names. This
script is the only thing that reads it, and it treats
`job_hunter_isolation.sql`'s array as the source of truth -- not a second
copy of the table list kept here, which could itself drift from the other
two. A table added to one side and not the other fails, naming the table
and both file paths, rather than passing quietly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
PGTAP_FILE = _REPO / "supabase" / "tests" / "pgtap" / "job_hunter_isolation.sql"
AGENTS_FILE = _REPO / "engine" / "AGENTS.md"

_PGTAP_VIEW = re.compile(
    r"create\s+view\s+pg_temp\.job_hunter_shared_tables\s+as\s*"
    r"select\s+unnest\s*\(\s*array\s*\[(?P<items>.*?)\]\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED_NAME = re.compile(r"'([^']+)'")

_DOC_MARKER = re.compile(
    r"<!--\s*job-hunter-shared-tables:\s*(?P<items>.*?)\s*-->", re.DOTALL
)


class MissingMarkerError(ValueError):
    """The source list or the doc marker was not found."""


def pgtap_shared_tables(sql_text: str) -> set[str]:
    """The enforced set, read from `pg_temp.job_hunter_shared_tables`."""
    match = _PGTAP_VIEW.search(sql_text)
    if not match:
        raise MissingMarkerError(
            f"no `pg_temp.job_hunter_shared_tables` view found in {PGTAP_FILE}"
        )
    return set(_QUOTED_NAME.findall(match.group("items")))


def documented_shared_tables(agents_md_text: str) -> set[str]:
    """The documented set, read from the `job-hunter-shared-tables` comment."""
    match = _DOC_MARKER.search(agents_md_text)
    if not match:
        raise MissingMarkerError(
            f"no `<!-- job-hunter-shared-tables: ... -->` comment found in {AGENTS_FILE}"
        )
    items = match.group("items")
    return {name.strip() for name in items.split(",") if name.strip()}


def check(pgtap_text: str, agents_text: str) -> list[str]:
    """Mismatch lines, empty when the two sets agree."""
    actual = pgtap_shared_tables(pgtap_text)
    documented = documented_shared_tables(agents_text)

    problems = []
    for table in sorted(actual - documented):
        problems.append(
            f"{table} is shared (in {PGTAP_FILE}'s job_hunter_shared_tables) "
            f"but missing from the job-hunter-shared-tables comment in {AGENTS_FILE}"
        )
    for table in sorted(documented - actual):
        problems.append(
            f"{table} is documented as shared in {AGENTS_FILE} but is not in "
            f"{PGTAP_FILE}'s job_hunter_shared_tables"
        )
    return problems


def main(argv: list[str]) -> int:
    del argv
    try:
        problems = check(PGTAP_FILE.read_text(), AGENTS_FILE.read_text())
    except MissingMarkerError as error:
        print(f"check_job_hunter_shared_tables_doc: {error}", file=sys.stderr)
        return 1

    if problems:
        print("check_job_hunter_shared_tables_doc: documented shared-table set is stale:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("check_job_hunter_shared_tables_doc: documented shared-table set matches the schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

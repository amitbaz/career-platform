#!/usr/bin/env python3
"""Run a pgTAP command on a staged copy of the suite plus this tree's tables.

    python3 scripts/pgtap_stage.py <command> [args...]

`supabase/tests/pgtap/job_hunter_isolation.sql` guards that every Job Hunter
table has an isolation check, measured against the tables this tree's
migrations create -- not against the database. The local Supabase stack is
shared by every worktree on the machine, so it also holds other branches'
unmerged tables, and measuring against it failed branches for changes they
did not contain (#207).

pgTAP cannot read the tree: `supabase test db` runs pg_prove in a container
that mounts only the directory it is given, so `supabase/migrations` is not
there. So this script replays the migrations on the host, copies
`supabase/tests/pgtap` into a fresh directory under the git-ignored
`supabase/.temp/`, writes the surviving public tables into it as
`tree_public_tables.txt`, runs <command> with that directory appended, and
deletes the directory afterwards. The list only ever exists beside the run
that derived it. A bare `supabase test db supabase/tests/pgtap` finds none and
the guard fails saying so, rather than trusting a list left over from another
branch.

The replay is textual, in version order: `create table` adds a name, `drop
table` removes it, `alter table ... rename to` renames it. Comments are
stripped first, outside string literals and dollar-quoted bodies, so a `/*`
inside a string cannot swallow what follows it. Temporary tables and other
schemas are left out.

Where the replay can be wrong it errs toward naming too many tables, because
the two errors are not symmetric: an extra name fails the guard loudly, a
missing one silently exempts a table from it. So a create counts wherever it
appears, and a drop only at the start of a statement -- `alter publication
... drop table` removes nothing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = _REPO / "supabase" / "migrations"
PGTAP = _REPO / "supabase" / "tests" / "pgtap"
#: Inside the repository rather than the system temp directory, because the
#: container runtime is only guaranteed to be able to mount the repository.
STAGING_ROOT = _REPO / "supabase" / ".temp"
TREE_TABLES_FILE = "tree_public_tables.txt"

#: The Supabase CLI's own migration filename pattern: a numeric version, an
#: underscore, a name. Anything else in the directory is not applied.
_MIGRATION_FILE = re.compile(r"^(\d+)_.*\.sql$")

#: One left-to-right pass, so whichever opens first wins: a `--` inside a
#: string is not a comment, and a quote inside a comment opens no string.
_LEXEME = re.compile(
    r"(?P<comment>--[^\n]*|/\*.*?\*/)"
    r"|(?<!\w)[eE]'(?:[^'\\]|\\.|'')*'"
    r"|'(?:[^']|'')*'"
    r"|\$(?P<tag>(?:[A-Za-z_]\w*)?)\$.*?\$(?P=tag)\$",
    re.DOTALL,
)


def _qualified_name(prefix: str) -> str:
    """A table name, optionally schema-qualified, either part optionally quoted."""
    return rf'(?:"?(?P<{prefix}schema>\w+)"?\s*\.\s*)?"?(?P<{prefix}name>\w+)"?'


_STATEMENT = re.compile(
    r"\bcreate\s+(?:(?:global\s+|local\s+)?(?P<temp>temp|temporary)\s+|unlogged\s+)?"
    r"table\s+(?:if\s+not\s+exists\s+)?" + _qualified_name("create_")
    + r"|(?:^|;)\s*drop\s+table\s+(?:if\s+exists\s+)?(?P<drop_list>[^;]+)"
    + r"|\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?" + _qualified_name("rename_")
    + r'\s+rename\s+to\s+"?(?P<new_name>\w+)"?',
    re.IGNORECASE,
)

_DROP_TARGET = re.compile("^" + _qualified_name("") + "$")


def _is_public(schema: str | None) -> bool:
    # An unqualified name lands in the first schema on the search path,
    # which for a migration is public.
    return schema is None or schema.lower() == "public"


def _strip_comments(sql: str) -> str:
    return _LEXEME.sub(lambda m: " " if m.group("comment") else m.group(0), sql)


def public_tables(migrations: Path) -> set[str]:
    """The public tables left standing after replaying `migrations` in version order.

    Raises ValueError when the directory holds no migrations: an empty set
    from a wrong path would let the guard compare nothing against nothing.
    """
    files = sorted(
        (p for p in migrations.glob("*.sql") if _MIGRATION_FILE.match(p.name)),
        key=lambda p: p.name,
    )
    if not files:
        raise ValueError(f"no migrations found in {migrations}")

    tables: set[str] = set()
    for path in files:
        for match in _STATEMENT.finditer(_strip_comments(path.read_text())):
            if match.group("create_name"):
                if not match.group("temp") and _is_public(match.group("create_schema")):
                    tables.add(match.group("create_name").lower())
            elif match.group("drop_list"):
                targets = re.sub(r"\s+(cascade|restrict)\s*$", "", match.group("drop_list"), flags=re.I)
                for target in targets.split(","):
                    parsed = _DROP_TARGET.match(target.strip())
                    if parsed and _is_public(parsed.group("schema")):
                        tables.discard(parsed.group("name").lower())
            elif _is_public(match.group("rename_schema")):
                old = match.group("rename_name").lower()
                if old in tables:
                    tables.discard(old)
                    tables.add(match.group("new_name").lower())
    return tables


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: pgtap_stage.py <command> [args...]", file=sys.stderr)
        return 2
    try:
        tables = sorted(public_tables(MIGRATIONS))
    except ValueError as error:
        print(f"pgtap_stage: {error}", file=sys.stderr)
        return 1

    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="pgtap-", dir=STAGING_ROOT))
    try:
        suite = stage / "pgtap"
        shutil.copytree(PGTAP, suite)
        (suite / TREE_TABLES_FILE).write_text("".join(f"{name}\n" for name in tables))
        print(f"pgtap_stage: {len(tables)} public tables from this tree's migrations; suite staged at {suite}", flush=True)
        return subprocess.call([*argv, str(suite)])
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

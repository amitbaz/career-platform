"""`scripts/pgtap_stage.py` gives the pgTAP isolation guard this tree's tables.

Why the guard needs the tree's set rather than the database's is in the
script's docstring (#207). Derivation is tested by importing the script;
staging by driving it in a subprocess with a stand-in for `supabase test db`.
Nothing here touches the Supabase stack.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "pgtap_stage.py"

_spec = importlib.util.spec_from_file_location("pgtap_stage", _SCRIPT)
pgtap_stage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pgtap_stage)


def _derive(tmp_path: Path, migrations: dict[str, str]) -> list[str]:
    for name, sql in migrations.items():
        (tmp_path / name).write_text(sql)
    return sorted(pgtap_stage.public_tables(tmp_path))


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, timeout=60
    )


def test_names_every_public_table_a_migration_creates(tmp_path):
    assert _derive(
        tmp_path,
        {
            "001_a.sql": "create table public.job_hunter_jobs (id uuid);\n"
            "create table public.profiles (id uuid);",
            "002_b.sql": "CREATE TABLE IF NOT EXISTS \"public\".\"job_hunter_quoted\" (id uuid);\n"
            "create unlogged table job_hunter_unqualified (id uuid);",
        },
    ) == ["job_hunter_jobs", "job_hunter_quoted", "job_hunter_unqualified", "profiles"]


def test_a_table_in_another_schema_is_not_public(tmp_path):
    assert _derive(
        tmp_path,
        {
            "001_a.sql": "create table private.user_provider_credentials (id uuid);\n"
            "create temporary table job_hunter_scratch (id uuid);\n"
            "create table public.job_hunter_jobs (id uuid);",
        },
    ) == ["job_hunter_jobs"]


def test_a_dropped_table_is_gone_and_a_recreated_one_is_back(tmp_path):
    assert _derive(
        tmp_path,
        {
            "001_a.sql": "create table public.job_hunter_old (id uuid);\n"
            "create table public.job_hunter_back (id uuid);",
            "002_b.sql": "drop table if exists public.job_hunter_old cascade;\n"
            "drop table public.job_hunter_back;",
            "003_c.sql": "create table public.job_hunter_back (id uuid);",
        },
    ) == ["job_hunter_back"]


def test_a_drop_naming_several_tables_drops_them_all(tmp_path):
    assert _derive(
        tmp_path,
        {
            "001_a.sql": "create table public.job_hunter_one (id uuid);\n"
            "create table public.job_hunter_two (id uuid);\n"
            "create table public.job_hunter_kept (id uuid);",
            "002_b.sql": "drop table public.job_hunter_one, job_hunter_two cascade;",
        },
    ) == ["job_hunter_kept"]


def test_dropping_a_table_from_a_publication_does_not_drop_the_table(tmp_path):
    assert _derive(
        tmp_path,
        {
            "001_a.sql": "create table public.job_hunter_live (id uuid);\n"
            "alter publication supabase_realtime drop table public.job_hunter_live;",
        },
    ) == ["job_hunter_live"]


def test_migrations_apply_in_version_order_not_listing_order(tmp_path):
    # Written in reverse so a directory listing that happened to return
    # creation order would apply the drop before the create.
    assert _derive(
        tmp_path,
        {
            "002_b.sql": "drop table public.job_hunter_gone;",
            "001_a.sql": "create table public.job_hunter_gone (id uuid);",
        },
    ) == []


def test_a_renamed_table_goes_by_its_new_name(tmp_path):
    assert _derive(
        tmp_path,
        {
            "001_a.sql": "create table public.job_hunter_before (id uuid);",
            "002_b.sql": "alter table public.job_hunter_before rename to job_hunter_after;",
        },
    ) == ["job_hunter_after"]


def test_commented_out_statements_create_nothing(tmp_path):
    assert _derive(
        tmp_path,
        {
            "001_a.sql": "-- create table public.job_hunter_line_comment (id uuid);\n"
            "/* create table public.job_hunter_block\n   comment (id uuid); */\n"
            "create table public.job_hunter_real (id uuid); -- trailing note",
        },
    ) == ["job_hunter_real"]


@pytest.mark.parametrize(
    "opener",
    [
        pytest.param("insert into t values ('/*');", id="comment opener inside a string"),
        pytest.param("comment on table t is $$it's$$;", id="apostrophe inside a dollar body"),
        pytest.param("select E'\\'';", id="escaped quote inside an E-string"),
    ],
)
def test_string_contents_never_decide_what_is_a_comment(tmp_path, opener):
    # Each opener, lexed naively, starts a comment or a string that runs on
    # and changes what the lines below it mean.
    assert _derive(
        tmp_path,
        {
            "001_a.sql": f"{opener}\n"
            "-- create table public.job_hunter_commented (id uuid);\n"
            "create table public.job_hunter_real (id uuid);\n"
            "select 'end'; -- */",
        },
    ) == ["job_hunter_real"]


def test_files_that_are_not_migrations_are_ignored(tmp_path):
    assert _derive(
        tmp_path,
        {
            "001_a.sql": "create table public.job_hunter_real (id uuid);",
            "README.md": "create table public.job_hunter_prose (id uuid);",
        },
    ) == ["job_hunter_real"]


def test_no_migrations_is_an_error_not_an_empty_tree(tmp_path):
    # An empty set would let the guard compare nothing against nothing.
    with pytest.raises(ValueError, match="no migrations found"):
        pgtap_stage.public_tables(tmp_path)


_PROBE = (
    "import json, os, sys\n"
    "suite = sys.argv[1]\n"
    "print(json.dumps({'suite': suite, 'files': sorted(os.listdir(suite)),\n"
    "  'tables': open(os.path.join(suite, 'tree_public_tables.txt')).read().split()}))\n"
    "sys.exit(3)\n"
)


def test_the_command_runs_on_a_staged_suite_holding_this_trees_tables():
    result = _run([sys.executable, "-c", _PROBE])

    assert result.returncode == 3, result.stderr  # the command's own exit code
    seen = json.loads(result.stdout.splitlines()[-1])
    assert "job_hunter_isolation.sql" in seen["files"]
    assert "job_hunter_jobs" in seen["tables"]
    # Dropped by 20260910080000_job_hunter_source_registry.sql.
    assert "job_hunter_search_api_usage" not in seen["tables"]


def test_the_list_never_outlives_its_run_or_lands_in_the_tree():
    result = _run([sys.executable, "-c", _PROBE])

    seen = json.loads(result.stdout.splitlines()[-1])
    assert not Path(seen["suite"]).exists()
    assert not (_REPO / "supabase" / "tests" / "pgtap" / "tree_public_tables.txt").exists()


def test_no_command_is_a_usage_error():
    result = _run([])

    assert result.returncode == 2
    assert "usage" in result.stderr

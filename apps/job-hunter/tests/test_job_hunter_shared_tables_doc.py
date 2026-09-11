"""`scripts/check_job_hunter_shared_tables_doc.py` guards AGENTS.md's shared-table
prose against `pg_temp.job_hunter_shared_tables` (#215).

Extraction is tested by importing the script; the last test runs it against
the real repository files, so a future table added to one side and not the
other fails here rather than passing quietly.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "check_job_hunter_shared_tables_doc.py"

_spec = importlib.util.spec_from_file_location("check_job_hunter_shared_tables_doc", _SCRIPT)
check_doc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_doc)

_PGTAP_SQL = """
create view pg_temp.job_hunter_shared_tables as
select unnest(array[
  'job_hunter_postings',
  'job_hunter_job_facets',
  'job_hunter_companies'
]) as table_name;
"""

_AGENTS_MD = """
are the three Job Hunter tables shared between users.
<!-- job-hunter-shared-tables: job_hunter_postings, job_hunter_job_facets, job_hunter_companies -->
"""


def test_pgtap_shared_tables_reads_the_array():
    assert check_doc.pgtap_shared_tables(_PGTAP_SQL) == {
        "job_hunter_postings",
        "job_hunter_job_facets",
        "job_hunter_companies",
    }


def test_documented_shared_tables_reads_the_comment():
    assert check_doc.documented_shared_tables(_AGENTS_MD) == {
        "job_hunter_postings",
        "job_hunter_job_facets",
        "job_hunter_companies",
    }


def test_matching_sets_produce_no_problems():
    assert check_doc.check(_PGTAP_SQL, _AGENTS_MD) == []


def test_a_table_missing_from_the_doc_is_named():
    sql = _PGTAP_SQL.replace(
        "'job_hunter_companies'", "'job_hunter_companies', 'job_hunter_ats_boards'"
    )
    problems = check_doc.check(sql, _AGENTS_MD)
    assert len(problems) == 1
    assert "job_hunter_ats_boards" in problems[0]
    assert "missing from the job-hunter-shared-tables comment" in problems[0]


def test_a_table_the_pgtap_list_no_longer_has_is_named():
    sql = _PGTAP_SQL.replace(",\n  'job_hunter_companies'", "")
    problems = check_doc.check(sql, _AGENTS_MD)
    assert len(problems) == 1
    assert "job_hunter_companies" in problems[0]
    assert "is not in" in problems[0]


def test_missing_pgtap_view_is_an_error():
    import pytest

    with pytest.raises(check_doc.MissingMarkerError, match="job_hunter_shared_tables"):
        check_doc.check("-- no view here", _AGENTS_MD)


def test_missing_doc_marker_is_an_error():
    import pytest

    with pytest.raises(check_doc.MissingMarkerError, match="job-hunter-shared-tables"):
        check_doc.check(_PGTAP_SQL, "no comment here")


def test_the_real_repository_files_agree():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)], capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stderr

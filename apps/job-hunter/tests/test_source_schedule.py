from __future__ import annotations

import pytest

from job_hunter.source_schedule import BANDS, cron_expression


@pytest.mark.parametrize("index", range(len(BANDS)))
def test_every_band_renders_a_five_field_cron_expression(index):
    expression = cron_expression(index, source_key="remotive")
    assert len(expression.split()) == 5


def test_two_sources_on_the_same_band_do_not_fire_at_the_same_minute():
    """Eighteen sources all firing on minute zero is a self-inflicted spike."""
    a = cron_expression(1, source_key="remotive")
    b = cron_expression(1, source_key="lever:acme")
    assert a != b


def test_the_offset_is_stable_for_a_given_source():
    assert cron_expression(1, source_key="remotive") == cron_expression(
        1, source_key="remotive"
    )


import re
from pathlib import Path


def _migration_text() -> str:
    """The migration that last defines job_hunter_reschedule_sources.

    Later migrations re-create the function (#258), and the definition that
    runs is the last one applied, so that is the one to hold against Python.
    """
    root = Path(__file__).resolve().parents[3]
    defining = [
        text
        for path in sorted(root.glob("supabase/migrations/*.sql"))
        if "function public.job_hunter_reschedule_sources()"
        in (text := path.read_text(encoding="utf-8"))
    ]
    assert defining, "no migration defines job_hunter_reschedule_sources"
    return defining[-1]


def test_the_sql_ladder_matches_the_python_one():
    """Two copies of one policy drift unless something holds them together."""
    sql = _migration_text()
    declared = re.search(r"v_bands\s+int\[\]\s*:=\s*array\[([^\]]+)\]", sql)
    assert declared, "job_hunter_reschedule_sources declares no band array"
    sql_bands = tuple(int(value.strip()) for value in declared.group(1).split(","))
    assert sql_bands == BANDS


def test_every_python_cron_rendering_appears_in_the_sql():
    """The format strings differ in syntax; the shapes they produce must not."""
    sql = _migration_text()
    for index, minutes in enumerate(BANDS):
        rendered = cron_expression(index, source_key="remotive")
        fields = rendered.split()
        # Compare the shape of the day/month/weekday fields, which is where a
        # cadence actually lives -- the minute and hour are per-source offsets.
        shape = " ".join(fields[2:])
        assert shape in sql, (
            f"band {minutes} renders day/month/weekday {shape!r}, "
            "which job_hunter_reschedule_sources does not produce"
        )

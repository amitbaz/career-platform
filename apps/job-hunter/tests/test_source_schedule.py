from __future__ import annotations

import pytest

from job_hunter.source_schedule import BANDS, cron_expression, next_band


def test_novelty_promotes_one_band():
    assert next_band(3, outcome="fetched", novelty=1) == 2


def test_an_empty_crawl_demotes_one_band():
    assert next_band(1, outcome="fetched", novelty=0) == 2


def test_not_modified_counts_as_empty():
    """An unchanged board is healthy but produced nothing; visit it less."""
    assert next_band(0, outcome="not_modified", novelty=0) == 1


def test_a_rate_limited_source_backs_off_even_when_it_returned_something():
    assert next_band(0, outcome="rate_limited", novelty=9) == 1


def test_a_failing_source_backs_off():
    assert next_band(2, outcome="failed", novelty=0) == 3


def test_the_fastest_band_is_a_floor():
    assert next_band(0, outcome="fetched", novelty=5) == 0


def test_the_slowest_band_is_a_ceiling():
    last = len(BANDS) - 1
    assert next_band(last, outcome="fetched", novelty=0) == last


def test_a_source_producing_nothing_converges_on_the_slowest_band():
    """Five consecutive empty crawls take the fastest source to the floor."""
    index = 0
    for _ in range(len(BANDS) - 1):
        index = next_band(index, outcome="fetched", novelty=0)
    assert BANDS[index] == 10080


def test_recovery_is_one_band_at_a_time_not_a_jump():
    index = len(BANDS) - 1
    index = next_band(index, outcome="fetched", novelty=3)
    assert index == len(BANDS) - 2


def test_an_unknown_outcome_is_rejected_rather_than_treated_as_healthy():
    with pytest.raises(ValueError):
        next_band(0, outcome="probably_fine", novelty=0)


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

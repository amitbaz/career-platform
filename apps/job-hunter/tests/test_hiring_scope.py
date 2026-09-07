"""Tests for the posting-only hiring-scope unit (issue #16).

Everything here is about a *posting*: which regions it says it hires in.
Nothing in this module knows about markets, candidates, or scoring -- that
separation is deliberate (see the module docstring in hiring_scope.py).
"""

from job_hunter.hiring_scope import (
    ASIA_PACIFIC,
    EUROPE,
    MIDDLE_EAST,
    NORTH_AMERICA,
    _compile,
    determine_hiring_scope,
    regions_for_locations,
    regions_in_text,
)
from job_hunter.models import Job


def _job(description: str, **overrides) -> Job:
    defaults = dict(
        source="ashby",
        title="Senior / Staff Product Engineer",
        company="Linear",
        location="Remote",
        remote=True,
        description=description,
    )
    defaults.update(overrides)
    return Job(**defaults)


# The posting from the 2026-09-05 run that motivated issue #16: one listing
# variant carried a North America location label, but the description says
# both regions are eligible.
LINEAR_DESCRIPTION = (
    "Linear is a fully remote company. This role is open to candidates based "
    "in the US and Europe, and can be performed from anywhere within those "
    "regions. You will work on the core product."
)


def test_explicit_multi_region_eligibility_yields_both_regions():
    scope = determine_hiring_scope(_job(LINEAR_DESCRIPTION, location="North America"))
    assert scope.regions == frozenset({NORTH_AMERICA, EUROPE})
    assert scope.is_explicit


def test_us_only_posting_yields_only_north_america():
    scope = determine_hiring_scope(
        _job(
            "This role is open to candidates based in the United States. "
            "We are unable to consider applicants elsewhere."
        )
    )
    assert scope.regions == frozenset({NORTH_AMERICA})


def test_incidental_company_prose_does_not_expand_eligibility():
    """Background prose names regions without describing who may be hired."""
    scope = determine_hiring_scope(
        _job(
            "Our engineering team spans Europe and North America. "
            "We serve customers in the US and across EMEA."
        )
    )
    assert scope.regions == frozenset()
    assert not scope.is_explicit


def test_colleagues_location_is_not_candidate_eligibility():
    scope = determine_hiring_scope(
        _job("You will collaborate daily with teams based in Europe and the US.")
    )
    assert scope.regions == frozenset()


def test_global_posting_stays_unscoped_so_attribution_fails_open():
    scope = determine_hiring_scope(
        _job("Fully remote. You can work from anywhere in the world.")
    )
    assert scope.regions == frozenset()
    assert not scope.is_explicit


def test_lowercase_us_pronoun_is_not_the_united_states():
    scope = determine_hiring_scope(
        _job("This role is open to candidates based in Germany. Come join us!")
    )
    assert scope.regions == frozenset({EUROPE})


def test_scope_records_the_clause_that_established_it():
    scope = determine_hiring_scope(_job(LINEAR_DESCRIPTION))
    assert any("open to candidates" in clause for clause in scope.evidence)


def test_posting_without_a_description_has_no_scope():
    scope = determine_hiring_scope(_job(""))
    assert scope.regions == frozenset()
    assert scope.evidence == ()


def test_right_to_work_language_is_eligibility_language():
    scope = determine_hiring_scope(
        _job("Applicants must have the right to work in the United Kingdom.")
    )
    assert scope.regions == frozenset({EUROPE})


def test_must_be_located_language_is_eligibility_language():
    scope = determine_hiring_scope(
        _job("You must be located in Israel; this is a fully remote position.")
    )
    assert scope.regions == frozenset({MIDDLE_EAST})


def test_broad_north_america_aliases_are_read_consistently():
    for phrase in ("the US", "the USA", "the United States", "North America"):
        scope = determine_hiring_scope(
            _job(f"This role is open to candidates based in {phrase}.")
        )
        assert scope.regions == frozenset({NORTH_AMERICA}), phrase


def test_broad_europe_aliases_are_read_consistently():
    for phrase in ("Europe", "the EU", "the European Union", "EMEA"):
        scope = determine_hiring_scope(
            _job(f"This role is open to candidates based in {phrase}.")
        )
        assert scope.regions == frozenset({EUROPE}), phrase


def test_regions_in_text_maps_market_location_names():
    assert regions_in_text("Berlin Germany Europe") == frozenset({EUROPE})
    assert regions_in_text("New York NYC San Francisco Bay Area") == frozenset(
        {NORTH_AMERICA}
    )
    assert regions_in_text("Israel Tel Aviv") == frozenset({MIDDLE_EAST})
    assert regions_in_text("Singapore") == frozenset({ASIA_PACIFIC})
    assert regions_in_text("London UK United Kingdom") == frozenset({EUROPE})


def test_regions_in_text_is_empty_when_no_region_is_named():
    """An unplaced location must map to no region, so it disqualifies nothing."""
    assert regions_in_text("Remote - Anywhere") == frozenset()
    assert regions_in_text("") == frozenset()


def test_negated_eligibility_language_is_not_read_as_eligibility():
    """A refusal to hire somewhere must not be read as an offer to.

    Dropping the clause (rather than trying to invert it) keeps the module
    conservative: the posting falls back to having stated nothing.
    """
    for description in (
        "We are not hiring in Europe at this time.",
        "This role is not open to candidates based in Europe.",
        "Unfortunately we cannot hire in Europe.",
    ):
        scope = determine_hiring_scope(_job(description))
        assert scope.regions == frozenset(), description


def test_unable_to_work_in_is_not_authorised_to_work_in():
    scope = determine_hiring_scope(
        _job("Candidates unable to work in the United States need not apply.")
    )
    assert scope.regions == frozenset()


def test_sentence_ending_in_an_undotted_acronym_still_ends_the_clause():
    """"...in the US." ends a sentence; the next sentence's regions are not scope."""
    scope = determine_hiring_scope(
        _job(
            "This role is open to candidates based in the US. Our engineering "
            "team is spread across Europe."
        )
    )
    assert scope.regions == frozenset({NORTH_AMERICA})


def test_a_dotted_acronym_does_not_end_the_clause():
    scope = determine_hiring_scope(
        _job("This role is open to candidates based in the U.S. and Europe.")
    )
    assert scope.regions == frozenset({NORTH_AMERICA, EUROPE})


def test_where_colleagues_live_is_not_where_candidates_may_live():
    scope = determine_hiring_scope(
        _job("Remote position. Our employees are located in the US and Germany.")
    )
    assert scope.regions == frozenset()


def test_a_region_denied_after_the_cue_does_not_enter_the_scope():
    scope = determine_hiring_scope(_job("We are hiring in Europe, not in the US."))
    assert scope.regions == frozenset({EUROPE})


def test_cue_offsets_survive_characters_that_change_length_when_lowercased():
    """`str.lower()` is not length-preserving; the slice must not drift."""
    scope = determine_hiring_scope(
        _job("İİİ - candidates must be located in Germany.")
    )
    assert scope.regions == frozenset({EUROPE})


def test_a_dotted_acronym_ending_a_sentence_still_ends_the_clause():
    """"...in the U.S." then a new capitalised sentence is two sentences."""
    scope = determine_hiring_scope(
        _job("Candidates must be located in the U.S. Our team spans Europe.")
    )
    assert scope.regions == frozenset({NORTH_AMERICA})


def test_someone_elses_hiring_is_not_this_employers_scope():
    scope = determine_hiring_scope(
        _job("Our customers are hiring across Europe and we help them do it.")
    )
    assert scope.regions == frozenset()


def test_first_person_hiring_language_is_still_read():
    scope = determine_hiring_scope(_job("We are currently hiring in Europe."))
    assert scope.regions == frozenset({EUROPE})


def test_a_remote_role_mentioning_a_region_is_not_an_eligibility_statement():
    scope = determine_hiring_scope(
        _job(
            "This is a remote role in a product team serving customers across "
            "Europe."
        )
    )
    assert scope.regions == frozenset()


def test_shouted_boilerplate_us_is_the_pronoun_not_the_country():
    scope = determine_hiring_scope(
        _job("You must be located in Germany. WORK WITH US and JOIN US today.")
    )
    assert scope.regions == frozenset({EUROPE})


def test_an_unrelated_negation_does_not_truncate_the_region_list():
    scope = determine_hiring_scope(
        _job(
            "This role is open to candidates based in the US, no relocation "
            "support, and Europe."
        )
    )
    assert scope.regions == frozenset({NORTH_AMERICA, EUROPE})


def test_configured_locations_read_acronyms_in_whatever_case_they_are_typed():
    """Market locations are configuration, not prose: "us"/"emea" are places."""
    assert regions_for_locations(("emea",)) == frozenset({EUROPE})
    assert regions_for_locations(("us",)) == frozenset({NORTH_AMERICA})
    assert regions_for_locations(("uk", "Berlin")) == frozenset({EUROPE})


def test_a_region_with_no_aliases_claims_nothing():
    """An empty alternation compiles to a pattern that matches everywhere."""
    assert _compile(()).search("anything at all") is None


def test_a_hyphenated_word_is_not_the_preposition_it_starts_with():
    scope = determine_hiring_scope(
        _job("We are open to in-office collaboration in our Berlin hub.")
    )
    assert scope.regions == frozenset()

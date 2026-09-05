from job_hunter.content_confidence import (
    AGGREGATOR_TEXT,
    CANONICAL_EMPLOYER_PAGE,
    OFFICIAL_ATS,
    PARTIAL_UNKNOWN,
    SOURCE_DETAIL_PAGE,
    infer_content_confidence,
    is_sufficient,
    tier_rank,
)


def test_ats_sources_infer_official_ats():
    assert infer_content_confidence("ashby", "Full JD text") == OFFICIAL_ATS
    assert infer_content_confidence("lever", "Full JD text") == OFFICIAL_ATS
    assert infer_content_confidence("greenhouse", "Full JD text") == OFFICIAL_ATS


def test_empty_description_is_always_partial_unknown_regardless_of_source():
    assert infer_content_confidence("ashby", "") == PARTIAL_UNKNOWN
    assert infer_content_confidence("ashby", "   ") == PARTIAL_UNKNOWN


def test_weak_and_unknown_sources():
    assert infer_content_confidence("hackernews", "some comment text") == AGGREGATOR_TEXT
    assert infer_content_confidence("wellfound", "full page body") == SOURCE_DETAIL_PAGE
    assert infer_content_confidence("some_future_source", "text") == AGGREGATOR_TEXT


def test_gmail_prefixed_sources_collapse_before_lookup():
    assert infer_content_confidence("gmail:abc123", "forwarded JD text") == AGGREGATOR_TEXT


def test_tier_rank_orders_best_to_worst():
    assert tier_rank(OFFICIAL_ATS) < tier_rank(CANONICAL_EMPLOYER_PAGE)
    assert tier_rank(CANONICAL_EMPLOYER_PAGE) < tier_rank(SOURCE_DETAIL_PAGE)
    assert tier_rank(SOURCE_DETAIL_PAGE) < tier_rank(AGGREGATOR_TEXT)
    assert tier_rank(AGGREGATOR_TEXT) < tier_rank(PARTIAL_UNKNOWN)


def test_tier_rank_treats_unset_as_worse_than_partial_unknown():
    assert tier_rank("") > tier_rank(PARTIAL_UNKNOWN)
    assert tier_rank("not_a_real_tier") > tier_rank(PARTIAL_UNKNOWN)


def test_is_sufficient():
    assert is_sufficient(OFFICIAL_ATS) is True
    assert is_sufficient(AGGREGATOR_TEXT) is True
    assert is_sufficient(PARTIAL_UNKNOWN) is False
    assert is_sufficient("") is False

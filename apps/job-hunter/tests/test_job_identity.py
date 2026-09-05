from job_hunter.job_identity import (
    company_identity_key,
    job_fallback_identity,
    locations_compatible,
    normalize_company_name,
    normalize_job_title,
)


def test_company_identity_ignores_safe_legal_suffix_and_punctuation():
    assert normalize_company_name("Acme GmbH") == "acme"
    assert normalize_company_name("ACME, GmbH") == "acme"
    assert company_identity_key("Acme GmbH") == company_identity_key("ACME")


def test_company_identity_keeps_meaningful_words():
    assert company_identity_key("Meta Platforms") != company_identity_key("Meta")


def test_title_normalization_is_exact_not_fuzzy():
    assert normalize_job_title(" Senior  Frontend Engineer ") == "senior frontend engineer"
    assert normalize_job_title("Senior Frontend Engineer") != normalize_job_title("Staff Frontend Engineer")


def test_fallback_identity_requires_company_and_title():
    assert job_fallback_identity("Acme", "Senior Frontend Engineer", "Berlin") == (
        "acme|senior frontend engineer|berlin"
    )
    assert job_fallback_identity("", "Senior Frontend Engineer", "Berlin") is None


def test_locations_compatible_allows_missing_or_contained_location():
    assert locations_compatible("", "Berlin") is True
    assert locations_compatible("Berlin, Germany", "Berlin") is True
    assert locations_compatible("Berlin", "New York") is False

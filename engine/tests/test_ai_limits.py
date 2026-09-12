import logging

from engine.ai.limits import CONSERVATIVE_FREE_TIER, free_tier_quota


def test_a_known_model_takes_its_published_free_tier_limits():
    quota = free_tier_quota("gemini-3.5-flash-lite")

    assert (quota.rpm, quota.tpm, quota.rpd) == (15, 250_000, 500)


def test_an_unknown_model_falls_back_to_the_most_conservative_known_limits(caplog):
    with caplog.at_level(logging.WARNING):
        quota = free_tier_quota("gemini-9.9-imaginary")

    assert (quota.rpm, quota.tpm, quota.rpd) == (
        CONSERVATIVE_FREE_TIER.rpm,
        CONSERVATIVE_FREE_TIER.tpm,
        CONSERVATIVE_FREE_TIER.rpd,
    )
    assert "gemini-9.9-imaginary" in caplog.text


def test_the_conservative_fallback_is_no_looser_than_any_table_entry():
    """A fallback that exceeded a real model's limit would not be a fallback."""
    from engine.ai.limits import FREE_TIER_LIMITS

    for limits in FREE_TIER_LIMITS.values():
        assert CONSERVATIVE_FREE_TIER.rpm <= limits.rpm
        assert CONSERVATIVE_FREE_TIER.tpm <= limits.tpm
        assert CONSERVATIVE_FREE_TIER.rpd <= limits.rpd


def test_each_override_replaces_exactly_one_published_default():
    quota = free_tier_quota("gemini-3.5-flash-lite", rpd=42)

    assert (quota.rpm, quota.tpm, quota.rpd) == (15, 250_000, 42)


def test_overrides_apply_to_an_unknown_model_without_a_second_fallback(caplog):
    with caplog.at_level(logging.WARNING):
        quota = free_tier_quota("gemini-9.9-imaginary", rpm=7, tpm=1_000, rpd=99)

    assert (quota.rpm, quota.tpm, quota.rpd) == (7, 1_000, 99)

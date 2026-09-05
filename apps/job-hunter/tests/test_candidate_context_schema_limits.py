from job_hunter.candidate_context import (
    CANDIDATE_CONTEXT_SCHEMA,
    _MAX_ITEM_LENGTH,
    _MAX_SUMMARY_LENGTH,
    _PREFERENCES_MAX_ITEM_LENGTH,
    _PREFERENCES_MAX_SUMMARY_LENGTH,
)


def test_candidate_context_schema_enforces_parser_array_limits():
    properties = CANDIDATE_CONTEXT_SCHEMA["properties"]

    preference_properties = properties["preferences"]["properties"]
    for name in (
        "preferred_roles",
        "preferred_seniority",
        "must_have_signals",
        "nice_to_have_signals",
        "preferred_locations",
        "avoid_signals",
    ):
        assert preference_properties[name]["maxItems"] == 8

    for name in (
        "technical_skills",
        "architecture_evidence",
        "leadership_ownership",
        "agentic_ai_evidence",
        "product_domain_evidence",
        "location_language_facts",
        "career_direction",
        "company_environment",
        "career_evidence",
    ):
        assert properties[name]["maxItems"] == 20


def test_candidate_context_schema_enforces_parser_string_length_limits():
    """Provider-facing bounds must match the local parser's `_validate_string_list`/
    summary checks, otherwise Gemini can return schema-valid JSON that our own
    validation then rejects (see issue #27, Run #30 fallback_error)."""
    properties = CANDIDATE_CONTEXT_SCHEMA["properties"]

    preference_properties = properties["preferences"]["properties"]
    for name in (
        "preferred_roles",
        "preferred_seniority",
        "must_have_signals",
        "nice_to_have_signals",
        "preferred_locations",
        "avoid_signals",
    ):
        assert preference_properties[name]["items"]["maxLength"] == _PREFERENCES_MAX_ITEM_LENGTH
    assert preference_properties["summary"]["maxLength"] == _PREFERENCES_MAX_SUMMARY_LENGTH

    for name in (
        "technical_skills",
        "architecture_evidence",
        "leadership_ownership",
        "agentic_ai_evidence",
        "product_domain_evidence",
        "location_language_facts",
        "career_direction",
        "company_environment",
        "career_evidence",
    ):
        assert properties[name]["items"]["maxLength"] == _MAX_ITEM_LENGTH
    assert properties["evaluation_summary"]["maxLength"] == _MAX_SUMMARY_LENGTH

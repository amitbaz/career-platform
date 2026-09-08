"""Objective facet extraction: prompt construction and response parsing.

The load-bearing constraint of issue #125 is that extraction cannot see who
is asking. Half of this file exists to hold that line: if the prompt could
carry candidate context, the facets it produces stop being shared and the
whole enrichment split is pointless.
"""

import inspect
import json

import pytest

from job_hunter import facets as facets_module
from job_hunter.content_confidence import OFFICIAL_ATS
from job_hunter.facets import (
    FacetExtractionError,
    PostingFacts,
    extract_facets,
    source_supplied_facets,
)
from job_hunter.hiring_scope import EUROPE, NORTH_AMERICA
from job_hunter.models import Job

#: Would only reach the prompt if some future change let candidate material in.
_CANDIDATE_SENTINEL = "CANDIDATE_CONTEXT_MUST_NOT_LEAK_4c81"


class FakeGemini:
    def __init__(self, text=""):
        self.text = text
        self.model = "gemini-2.5-flash-lite"
        self.prompts = []

    def generate_text(
        self,
        prompt,
        *,
        call_class,
        purpose=None,
        thinking_level=None,
        max_output_tokens=None,
        json_mode=False,
        json_schema=None,
        max_attempts=1,
    ):
        self.prompts.append((prompt, purpose, json_mode, max_attempts))
        return self.text


def _job(**overrides):
    defaults = dict(
        source="remotive",
        title="Senior Product Engineer",
        company="Acme",
        location="Remote (Europe)",
        description="We are hiring a senior engineer. React and TypeScript required.",
        content_confidence=OFFICIAL_ATS,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _payload(**overrides):
    payload = {
        "seniority": "senior",
        "remote_policy": "remote",
        "relocation_policy": "not_offered",
        "hiring_regions": [EUROPE],
        "stack": ["react", "typescript"],
        "compensation": {
            "disclosed": True,
            "currency": "EUR",
            "minimum": 90000,
            "maximum": 120000,
            "period": "year",
        },
        "requirements": [
            {"requirement": "React", "depth": "experience", "kind": "must_have"},
            {"requirement": "GraphQL", "depth": "familiarity", "kind": "preferred"},
        ],
    }
    payload.update(overrides)
    return payload


def _gemini_for(payload_overrides=None):
    return FakeGemini(json.dumps(_payload(**(payload_overrides or {}))))


# --- The interface cannot carry a candidate -------------------------------------


def test_extract_facets_has_no_parameter_a_candidate_could_arrive_through():
    parameters = set(inspect.signature(extract_facets).parameters)
    assert parameters == {"posting", "ai"}


def test_posting_facts_carries_only_the_postings_own_fields():
    fields = set(PostingFacts.__dataclass_fields__)
    assert fields == {
        "title",
        "company",
        "location",
        "remote",
        "description",
        "content_confidence",
        "source",
        "stated_hiring_regions",
    }


def test_facets_module_never_imports_candidate_aware_types():
    # Convention would not survive a refactor; the import graph will.
    source = inspect.getsource(facets_module)
    for forbidden in (
        "CandidateContext",
        "CandidatePreferences",
        "SearchPolicy",
        "MarketPolicy",
        "candidate_context",
        "preferences",
    ):
        assert forbidden not in source, f"{forbidden} must not be reachable from facets.py"


def test_the_same_posting_always_produces_the_same_prompt():
    # This is what makes a facet shared rather than per-user: the prompt is a
    # function of the posting and nothing else, so two users asking about the
    # same posting ask the identical question and can reuse one answer.
    posting = PostingFacts.from_job(_job())
    first, second = _gemini_for(), _gemini_for()

    extract_facets(posting, first)
    extract_facets(PostingFacts.from_job(_job()), second)

    assert first.prompts[0][0] == second.prompts[0][0]


def test_the_prompt_is_built_from_the_postings_own_fields():
    gemini = _gemini_for()
    posting = PostingFacts.from_job(
        _job(description=f"Requires React. {_CANDIDATE_SENTINEL}")
    )
    extract_facets(posting, gemini)
    prompt = gemini.prompts[0][0]
    assert "Senior Product Engineer" in prompt
    assert "Acme" in prompt
    assert _CANDIDATE_SENTINEL in prompt


def test_extraction_uses_its_own_provider_purpose():
    gemini = _gemini_for()
    extract_facets(PostingFacts.from_job(_job()), gemini)
    _prompt, purpose, json_mode, _attempts = gemini.prompts[0]
    assert purpose == "job_facets"
    assert json_mode is True


# --- Parsing ---------------------------------------------------------------------


def test_parses_every_facet_from_a_well_formed_response():
    gemini = _gemini_for()
    result = extract_facets(PostingFacts.from_job(_job()), gemini)

    assert result.seniority == "senior"
    assert result.remote_policy == "remote"
    assert result.relocation_policy == "not_offered"
    assert result.hiring_regions == [EUROPE]
    assert result.stack == ["react", "typescript"]
    assert result.compensation.disclosed is True
    assert result.compensation.currency == "EUR"
    assert result.compensation.minimum == 90000
    assert result.compensation.maximum == 120000
    assert result.compensation.period == "year"
    assert result.requirements == [
        {"requirement": "React", "depth": "experience", "kind": "must_have"},
        {"requirement": "GraphQL", "depth": "familiarity", "kind": "preferred"},
    ]
    assert result.model == "gemini-2.5-flash-lite"


def test_parses_a_fenced_response():
    payload = json.dumps(_payload())
    gemini = FakeGemini(f"```json\n{payload}\n```")
    result = extract_facets(PostingFacts.from_job(_job()), gemini)
    assert result.seniority == "senior"


@pytest.mark.parametrize(
    "overrides",
    [
        {"seniority": "very senior"},
        {"remote_policy": "sometimes"},
        {"relocation_policy": "maybe"},
        {"hiring_regions": ["atlantis"]},
        {"hiring_regions": "europe"},
        {"stack": [""]},
        {"requirements": [{"requirement": "React", "depth": "guru", "kind": "must_have"}]},
        {"requirements": [{"requirement": "React", "depth": "experience", "kind": "wanted"}]},
        {"requirements": [{"requirement": "", "depth": "experience", "kind": "must_have"}]},
        {"compensation": {"disclosed": True, "currency": "EUR", "minimum": 120000,
                          "maximum": 90000, "period": "year"}},
        {"compensation": {"disclosed": True, "currency": "EUR", "minimum": -1,
                          "maximum": 90000, "period": "year"}},
        {"compensation": {"disclosed": True, "currency": "EUR", "minimum": 90000,
                          "maximum": 120000, "period": "fortnight"}},
    ],
)
def test_rejects_a_response_outside_the_facet_vocabulary(overrides):
    gemini = _gemini_for(overrides)
    with pytest.raises(FacetExtractionError):
        extract_facets(PostingFacts.from_job(_job()), gemini)


def test_rejects_unparseable_output():
    gemini = FakeGemini("not json at all")
    with pytest.raises(FacetExtractionError):
        extract_facets(PostingFacts.from_job(_job()), gemini)


def test_rejects_a_response_missing_a_requested_facet():
    payload = _payload()
    del payload["seniority"]
    gemini = FakeGemini(json.dumps(payload))
    with pytest.raises(FacetExtractionError):
        extract_facets(PostingFacts.from_job(_job()), gemini)


def test_undisclosed_compensation_carries_no_figures():
    gemini = _gemini_for(
        {"compensation": {"disclosed": False, "currency": "", "minimum": None,
                          "maximum": None, "period": ""}}
    )
    result = extract_facets(PostingFacts.from_job(_job()), gemini)
    assert result.compensation.disclosed is False
    assert result.compensation.minimum is None
    assert result.compensation.maximum is None


def test_disclosed_compensation_must_carry_a_figure():
    gemini = _gemini_for(
        {"compensation": {"disclosed": True, "currency": "EUR", "minimum": None,
                          "maximum": None, "period": "year"}}
    )
    with pytest.raises(FacetExtractionError):
        extract_facets(PostingFacts.from_job(_job()), gemini)


# --- Structured source data is used before the model is asked ---------------------


def test_ashby_remote_flag_supplies_the_remote_policy_without_the_model():
    posting = PostingFacts.from_job(_job(source="ashby", remote=True))
    assert source_supplied_facets(posting) == {"remote_policy": "remote"}


def test_greenhouses_derived_remote_flag_is_left_to_the_model():
    # Greenhouse's adapter derives `remote` from the substring "remote" in the
    # location label, so "Hybrid Remote - Berlin" arrives as True. Treating
    # that as a supplied fact would pin a hybrid role as fully remote forever,
    # because a supplied facet is never asked of the model.
    posting = PostingFacts.from_job(
        _job(source="greenhouse", remote=True, location="Hybrid Remote - Berlin")
    )
    assert "remote_policy" not in source_supplied_facets(posting)


def test_a_structured_not_remote_flag_is_left_to_the_model():
    # `isRemote: false` distinguishes neither hybrid nor onsite, so it is not
    # a facet -- recording it as "onsite" would invent a fact the source
    # never stated.
    posting = PostingFacts.from_job(_job(source="ashby", remote=False))
    assert "remote_policy" not in source_supplied_facets(posting)


def test_a_remote_flag_from_an_unstructured_source_is_left_to_the_model():
    posting = PostingFacts.from_job(_job(source="hackernews", remote=True))
    assert "remote_policy" not in source_supplied_facets(posting)


def test_an_explicitly_stated_hiring_scope_is_read_without_the_model():
    posting = PostingFacts.from_job(
        _job(description="We are open to candidates based in Germany and Canada.")
    )
    supplied = source_supplied_facets(posting)
    assert set(supplied["hiring_regions"]) == {EUROPE, NORTH_AMERICA}


def test_a_posting_that_states_no_scope_leaves_regions_to_the_model():
    posting = PostingFacts.from_job(_job(description="Our team spans Europe."))
    assert "hiring_regions" not in source_supplied_facets(posting)


def test_the_model_is_not_asked_for_a_facet_the_source_already_supplied():
    payload = _payload()
    del payload["remote_policy"]
    gemini = FakeGemini(json.dumps(payload))
    posting = PostingFacts.from_job(_job(source="ashby", remote=True))

    result = extract_facets(posting, gemini)

    prompt = gemini.prompts[0][0]
    assert "remote_policy" not in prompt
    assert result.remote_policy == "remote"
    assert "remote_policy" in result.source_supplied


def test_a_source_supplied_facet_survives_a_model_that_contradicts_it():
    gemini = _gemini_for({"remote_policy": "onsite"})
    posting = PostingFacts.from_job(_job(source="ashby", remote=True))
    assert extract_facets(posting, gemini).remote_policy == "remote"


def test_extraction_records_which_facets_came_free():
    gemini = _gemini_for()
    posting = PostingFacts.from_job(
        _job(source="ashby", remote=True,
             description="Applicants must be located in Germany.")
    )
    result = extract_facets(posting, gemini)
    assert sorted(result.source_supplied) == ["hiring_regions", "remote_policy"]
    assert result.hiring_regions == [EUROPE]


@pytest.mark.parametrize(
    "overrides, expectation",
    [
        ({"seniority": "Senior"}, lambda r: r.seniority == "senior"),
        ({"remote_policy": " Remote "}, lambda r: r.remote_policy == "remote"),
        ({"hiring_regions": ["Europe"]}, lambda r: r.hiring_regions == [EUROPE]),
        ({"stack": ["React", "react", "TypeScript"]},
         lambda r: r.stack == ["react", "typescript"]),
        ({"requirements": [{"requirement": "React", "depth": "Experience",
                            "kind": "Must_Have"}]},
         lambda r: r.requirements[0]["depth"] == "experience"),
        ({"compensation": {"disclosed": True, "currency": "eur", "minimum": 90000.0,
                           "maximum": "120000", "period": "Year"}},
         lambda r: r.compensation.minimum == 90000 and r.compensation.maximum == 120000
         and r.compensation.currency == "EUR" and r.compensation.period == "year"),
    ],
)
def test_casing_and_number_shape_drift_does_not_discard_the_whole_response(
    overrides, expectation
):
    # A rejected value fails the entire response, so a capitalisation would
    # throw away six correctly-read facets. Normalise, then validate.
    gemini = _gemini_for(overrides)
    assert expectation(extract_facets(PostingFacts.from_job(_job()), gemini))


def test_a_fractional_pay_figure_is_not_silently_rounded():
    gemini = _gemini_for({"compensation": {"disclosed": True, "currency": "EUR",
                                           "minimum": 90000.5, "maximum": 120000,
                                           "period": "year"}})
    with pytest.raises(FacetExtractionError):
        extract_facets(PostingFacts.from_job(_job()), gemini)

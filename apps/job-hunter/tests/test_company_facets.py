"""Company facet extraction: prompt construction and response parsing.

The load-bearing constraint of issue #198 is the same one #125 imposed on the
posting: extraction cannot see who is asking. A company's facts are the
cheapest cache the engine has -- amortised over every role that employer
posts, for months, across every user -- and the moment the prompt could carry
candidate context that entire saving disappears.
"""

import inspect
import json

import pytest

from job_hunter import company_facets as company_facets_module
from job_hunter.ai import (
    AIBudgetExceeded,
    AIQuotaPaused,
    AITemporaryCapacity,
    CallClass,
    CredentialUnavailable,
    PlatformAllowanceExhausted,
)
from job_hunter.company_facets import (
    CompanyEvidence,
    CompanyFacetExtractionError,
    extract_company_facets,
    source_supplied_company_facets,
)
from job_hunter.hiring_scope import EUROPE, NORTH_AMERICA
from job_hunter.models import Job

#: Would only reach the prompt if some future change let candidate material in.
_CANDIDATE_SENTINEL = "CANDIDATE_CONTEXT_MUST_NOT_LEAK_9b42"

_WELL_FORMED = {
    "industry": "fintech",
    "business_model": "b2b_saas",
    "stage": "series_a",
    "size_band": "51_200",
    "headquarters_region": "europe",
}


class FakeGemini:
    def __init__(self, text=""):
        self.text = text
        self.model = "gemini-2.5-flash-lite"
        self.prompts = []
        self.schemas = []
        self.call_classes = []
        self.purposes = []

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
        self.prompts.append(prompt)
        self.schemas.append(json_schema)
        self.call_classes.append(call_class)
        self.purposes.append(purpose)
        return self.text


class RaisingGemini(FakeGemini):
    def __init__(self, error):
        super().__init__()
        self._error = error

    def generate_text(self, prompt, **kwargs):
        raise self._error


class SequenceGemini(FakeGemini):
    """Answers each call from a list, so a parse retry can be observed."""

    def __init__(self, texts):
        super().__init__()
        self._texts = list(texts)

    def generate_text(self, prompt, **kwargs):
        super().generate_text(prompt, **kwargs)
        return self._texts.pop(0)


def _job(**overrides) -> Job:
    values = dict(
        source="greenhouse",
        title="Senior Product Engineer",
        company="Acme Payments Ltd",
        location="Berlin, Germany",
        url="https://boards.greenhouse.io/acmepayments/jobs/1",
        description="Acme Payments builds billing infrastructure for European marketplaces.",
        remote=True,
    )
    values.update(overrides)
    return Job(**values)


def _evidence(*jobs) -> CompanyEvidence:
    jobs = jobs or (_job(),)
    return CompanyEvidence.from_postings(jobs[0].company, list(jobs))


def _gemini_for(**overrides) -> FakeGemini:
    payload = dict(_WELL_FORMED)
    payload.update(overrides)
    return FakeGemini(json.dumps(payload))


# --------------------------------------------------------------------------
# The boundary: nothing per-user may reach this module
# --------------------------------------------------------------------------


def test_company_evidence_carries_only_the_employers_own_fields():
    fields = set(CompanyEvidence.__dataclass_fields__)
    assert fields == {
        "identity",
        "display_name",
        "employer_hosts",
        "ats_providers",
        "posting_titles",
        "posting_locations",
        "posting_excerpt",
    }


def test_company_facets_module_never_imports_candidate_aware_types():
    # Convention would not survive a refactor; the import graph will.
    source = inspect.getsource(company_facets_module)
    for forbidden in (
        "CandidateContext",
        "CandidatePreferences",
        "CompanyPreferences",
        "SearchPolicy",
        "MarketPolicy",
        "candidate_context",
        "preferences",
    ):
        assert forbidden not in source, f"{forbidden} must not reach company_facets.py"


def test_the_prompt_cannot_carry_candidate_context():
    # The prompt is a function of the evidence and nothing else, which is what
    # lets one answer serve every user.
    gemini = _gemini_for()
    extract_company_facets(_evidence(), gemini)

    assert _CANDIDATE_SENTINEL not in gemini.prompts[0]
    assert "candidate" not in gemini.prompts[0].lower()


def test_the_same_company_always_produces_the_same_prompt():
    # Two users asking about the same employer ask the identical question, so
    # they can reuse one answer -- and so can the same user tomorrow.
    first, second = _gemini_for(), _gemini_for()

    extract_company_facets(_evidence(_job(), _job(title="Staff Engineer")), first)
    extract_company_facets(_evidence(_job(title="Staff Engineer"), _job()), second)

    assert first.prompts[0] == second.prompts[0]


def test_extraction_is_funded_by_the_platform_key():
    gemini = _gemini_for()

    extract_company_facets(_evidence(), gemini)

    assert gemini.call_classes == [CallClass.SHARED_EXTRACTION]
    assert gemini.purposes == ["company_facets"]


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_identity_strips_a_legal_suffix_so_one_employer_is_one_company():
    # normalize_company_name, not normalize_text: under the latter "Acme Ltd"
    # and "Acme" would be two employers and each would be read once.
    assert _evidence(_job(company="Acme Payments Ltd")).identity == "acme payments"
    assert _evidence(_job(company="Acme Payments")).identity == "acme payments"


def test_the_display_name_keeps_what_a_person_would_recognise():
    assert _evidence(_job(company="Acme Payments Ltd")).display_name == "Acme Payments Ltd"


# --------------------------------------------------------------------------
# Structured and cheap sources first
# --------------------------------------------------------------------------


def test_a_country_domain_the_employer_owns_supplies_the_headquarters_region():
    evidence = _evidence(_job(url="https://acme-payments.de/careers/1"))

    assert source_supplied_company_facets(evidence) == {"headquarters_region": EUROPE}


def test_the_company_name_may_be_written_without_its_hyphen():
    evidence = _evidence(_job(url="https://careers.acmepayments.de/1"))

    assert source_supplied_company_facets(evidence) == {"headquarters_region": EUROPE}


def test_a_job_boards_country_domain_supplies_nothing():
    # The failure this rule exists to prevent: a US company whose only
    # posting the engine holds came from an Israeli board would otherwise be
    # recorded as headquartered in the Middle East -- and because a supplied
    # fact is never asked of the model, nothing would correct it, for every
    # user, until the refresh interval re-derived the same wrong answer.
    evidence = _evidence(_job(url="https://devjobs.co.il/jobs/123"))

    assert evidence.employer_hosts == ()
    assert source_supplied_company_facets(evidence) == {}


def test_a_domain_that_does_not_spell_the_company_name_supplies_nothing():
    # Fails open rather than guessing: "acmepay.de" may well be Acme
    # Payments, and may equally be somebody else.
    evidence = _evidence(_job(url="https://acmepay.de/careers/1"))

    assert source_supplied_company_facets(evidence) == {}


def test_a_supplied_fact_is_never_asked_of_the_model():
    gemini = _gemini_for()

    facets = extract_company_facets(
        _evidence(_job(company="Acme", url="https://acme.us/careers/1")), gemini
    )

    assert "headquarters_region" not in gemini.prompts[0]
    assert facets.headquarters_region == NORTH_AMERICA
    assert facets.source_supplied == ["headquarters_region"]
    assert set(gemini.schemas[0]["properties"]) == {
        "industry",
        "business_model",
        "stage",
        "size_band",
    }


def test_an_ats_vendors_domain_supplies_nothing():
    # boards.greenhouse.io says where the vendor is, not where its customer
    # is, and a country-domiciled ATS would otherwise place every one of its
    # customers in one region.
    assert source_supplied_company_facets(_evidence()) == {}


def test_two_employer_domains_that_disagree_supply_nothing():
    evidence = _evidence(
        _job(company="Acme", url="https://acme.de/careers/1"),
        _job(company="Acme", url="https://acme.jp/careers/2", source_job_id="2"),
    )

    assert source_supplied_company_facets(evidence) == {}


def test_a_generic_domain_supplies_nothing():
    evidence = _evidence(_job(company="Acme", url="https://acme.com/careers/1"))

    assert evidence.employer_hosts == ("acme.com",)
    assert source_supplied_company_facets(evidence) == {}


# --------------------------------------------------------------------------
# Parsing: the controlled vocabulary is the contract
# --------------------------------------------------------------------------


def test_a_well_formed_response_parses_into_the_vocabulary():
    facets = extract_company_facets(_evidence(), _gemini_for())

    assert facets.industry == "fintech"
    assert facets.business_model == "b2b_saas"
    assert facets.stage == "series_a"
    assert facets.size_band == "51_200"
    assert facets.headquarters_region == "europe"
    assert facets.identity == "acme payments"
    assert facets.model == "gemini-2.5-flash-lite"


def test_casing_and_punctuation_do_not_throw_away_the_whole_response():
    # A rejected value fails every facet with it, so "B2B SaaS" must not cost
    # four correctly-read facts over a capitalisation.
    facets = extract_company_facets(
        _evidence(), _gemini_for(business_model="B2B SaaS", stage="Series-A")
    )

    assert facets.business_model == "b2b_saas"
    assert facets.stage == "series_a"


def test_an_out_of_vocabulary_value_is_rejected_rather_than_stored():
    gemini = _gemini_for(industry="quantum llama farming")

    with pytest.raises(CompanyFacetExtractionError):
        extract_company_facets(_evidence(), gemini)


def test_an_unknown_is_a_value_and_not_a_rejection():
    facets = extract_company_facets(
        _evidence(),
        _gemini_for(industry="unknown", stage="unknown", size_band="unknown"),
    )

    assert facets.industry == "unknown"
    assert facets.is_known() is True  # business_model and region still say something


def test_a_company_read_as_entirely_unknown_reports_itself_as_unknown():
    facets = extract_company_facets(
        _evidence(),
        _gemini_for(
            industry="unknown",
            business_model="unknown",
            stage="unknown",
            size_band="unknown",
            headquarters_region="unknown",
        ),
    )

    assert facets.is_known() is False


def test_invalid_json_is_rejected():
    with pytest.raises(CompanyFacetExtractionError):
        extract_company_facets(_evidence(), FakeGemini("not json at all"))


def test_a_fenced_response_is_still_read():
    gemini = FakeGemini("```json\n" + json.dumps(_WELL_FORMED) + "\n```")

    assert extract_company_facets(_evidence(), gemini).industry == "fintech"


def test_one_unreadable_sample_is_retried_before_giving_up():
    gemini = SequenceGemini(["{", json.dumps(_WELL_FORMED)])

    facets = extract_company_facets(_evidence(), gemini)

    assert facets.industry == "fintech"
    assert len(gemini.prompts) == 2


# --------------------------------------------------------------------------
# Provider refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        AIBudgetExceeded("daily ceiling"),
        AIQuotaPaused(
            "paused", paused_until="2026-09-09T00:00:00+00:00", reason="daily_quota"
        ),
        CredentialUnavailable("no platform key"),
    ],
)
def test_a_platform_refusal_is_one_exhaustion_for_every_caller(error):
    with pytest.raises(PlatformAllowanceExhausted):
        extract_company_facets(_evidence(), RaisingGemini(error))


def test_rolling_capacity_passes_through_untranslated():
    # The caller decides whether to wait; this is not exhaustion.
    with pytest.raises(AITemporaryCapacity):
        extract_company_facets(
            _evidence(), RaisingGemini(AITemporaryCapacity("full", retry_after_seconds=1))
        )

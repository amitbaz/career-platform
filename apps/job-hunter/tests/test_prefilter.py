import pytest
from job_hunter.market_policy import attribute_market, market_by_id
from job_hunter.models import Job, SearchPolicy
from job_hunter.prefilter import prefilter_job
from tests.market_fixtures import make_market_policy


@pytest.fixture
def policy():
    return SearchPolicy(
        target_titles=["senior product engineer", "senior frontend engineer"],
        positive_keywords=["react", "typescript"],
        blocked_title_keywords=["junior", "qa", "devops"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        engineering_title_keywords=["engineer", "developer"],
        engineering_title_phrases=[
            "technical lead",
            "frontend lead",
            "software architect",
        ],
        blocked_profession_title_phrases=[
            "product manager",
            "product designer",
            "sales engineer",
            "data engineer",
            "machine learning engineer",
            "ios engineer",
        ],
    )


def test_prefilter_blocks_explicit_non_remote(policy):
    result = prefilter_job(
        Job(source="x", title="Senior Frontend Engineer", location="Berlin - onsite", remote=False),
        policy,
    )
    assert result.hard_blocker is True
    assert result.reason_code == "not_remote"
    assert "remote" in result.reason.lower()


def test_prefilter_blocks_blocked_title(policy):
    result = prefilter_job(
        Job(source="x", title="Junior Frontend Engineer", description="React TypeScript"),
        policy,
    )
    assert result.hard_blocker is True
    assert result.reason_code == "blocked_title"


def test_prefilter_keeps_relevant_remote_role(policy):
    result = prefilter_job(
        Job(source="x", title="Senior Product Engineer", description="React TypeScript remote product ownership"),
        policy,
    )
    assert result.should_evaluate is True
    assert result.hard_blocker is False
    assert result.reason_code == "passed"


def test_prefilter_passes_ambiguous_location(policy):
    result = prefilter_job(
        Job(source="x", title="Senior Frontend Engineer", location=""),
        policy,
    )
    assert result.hard_blocker is False
    assert result.reason_code == "passed"


def test_prefilter_blocks_unrelated_title_and_no_keywords(policy):
    result = prefilter_job(
        Job(source="x", title="Data Scientist ML Research", description="Python pandas sklearn"),
        policy,
    )
    assert result.should_evaluate is False
    assert result.reason_code == "off_target_profession"


@pytest.mark.parametrize(
    "title",
    [
        "Senior Product Engineer",
        "Staff Frontend Engineer",
        "Senior Software Engineer, Product",
        "Founding Engineer",
        "Frontend Developer",
        "Frontend Technical Lead",
    ],
)
def test_prefilter_accepts_software_engineering_professions(policy, title):
    result = prefilter_job(
        Job(source="x", title=title, description="React TypeScript product ownership", remote=True),
        policy,
    )
    assert result.should_evaluate is True


@pytest.mark.parametrize(
    "title",
    [
        "Senior Product Manager",
        "Platform Product Manager",
        "Technical Product Manager",
        "Senior Product Designer",
        "Product Designer, AI",
        "Senior Sales Engineer",
        "Senior Data Engineer",
        "Machine Learning Engineer",
        "Senior iOS Engineer",
    ],
)
def test_prefilter_rejects_off_target_professions_even_with_positive_keywords(policy, title):
    result = prefilter_job(
        Job(
            source="x",
            title=title,
            description="React TypeScript SaaS product ownership architecture",
            remote=True,
        ),
        policy,
    )
    assert result.should_evaluate is False
    assert result.reason_code == "off_target_profession"


def test_prefilter_blocked_title_wins_before_profession_acceptance(policy):
    result = prefilter_job(
        Job(source="x", title="Junior Frontend Engineer", description="React TypeScript", remote=True),
        policy,
    )
    assert result.should_evaluate is False
    assert result.reason_code == "blocked_title"


def test_prefilter_without_market_still_blocks_non_remote(policy):
    result = prefilter_job(
        Job(source="x", title="Senior Frontend Engineer", location="Berlin - onsite", remote=False),
        policy,
        market=None,
    )
    assert result.hard_blocker is True
    assert result.reason_code == "not_remote"


def test_prefilter_with_market_delegates_incompatibility_to_market_eligibility():
    market_policy = make_market_policy()
    market = market_by_id(market_policy, "london")
    result = prefilter_job(
        Job(
            source="x",
            title="Senior Frontend Engineer",
            location="London",
            description="We are unable to sponsor a visa for this role.",
        ),
        market_policy,
        market=market,
    )
    assert result.should_evaluate is False
    assert result.hard_blocker is True
    assert result.reason_code == "sponsorship_unavailable"


def test_prefilter_with_market_no_longer_hard_blocks_onsite_for_a_market_that_allows_it():
    market_policy = make_market_policy()
    market = market_by_id(market_policy, "london")
    result = prefilter_job(
        Job(
            source="x",
            title="Senior Frontend Engineer",
            location="London - onsite",
            description="React TypeScript product ownership",
            remote=False,
        ),
        market_policy,
        market=market,
    )
    assert result.should_evaluate is True
    assert result.reason_code == "passed"


def test_prefilter_with_market_still_applies_blocked_title_and_relevance_checks():
    market_policy = make_market_policy()
    market = market_by_id(market_policy, "germany_eu")
    result = prefilter_job(
        Job(source="x", title="Junior Frontend Engineer", location="Berlin", description="React TypeScript"),
        market_policy,
        market=market,
    )
    assert result.should_evaluate is False
    assert result.reason_code == "blocked_title"


@pytest.mark.parametrize(
    ("location", "description"),
    [
        ("Bangalore, India (Onsite)", "React TypeScript"),
        ("Austin, TX", "Onsite 5 days a week. React TypeScript"),
    ],
)
def test_unattributable_non_remote_job_is_hard_blocked_as_not_remote(location, description):
    """A non-remote job that no configured market claims stays unattributed
    and must fall through to the legacy non-remote hard blocker instead of
    being silently passed through on a first-enabled-market fallback."""
    market_policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Frontend Engineer",
        location=location,
        description=description,
        remote=False,
    )
    market_id = attribute_market(job, market_policy.markets)
    assert market_id is None

    result = prefilter_job(job, market_policy, market=None)
    assert result.should_evaluate is False
    assert result.hard_blocker is True
    assert result.reason_code == "not_remote"

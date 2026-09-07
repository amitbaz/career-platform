import base64

import pytest

from job_hunter.config import (
    ProfileNotFoundError,
    _parse_manual_company_watch,
    load_gmail_settings,
    load_settings,
)
from job_hunter.models import CompanyWatchSeed
from job_hunter.postgres_store import PostgresJobStore
from job_hunter.search_profile import SearchProfile, SearchProfileMarket
from tests.fake_supabase_client import FakeSupabaseClient


def _set_required_bot_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv(
        "CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode()
    )
    monkeypatch.setenv(
        "COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode()
    )
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")


@pytest.fixture(autouse=True)
def _set_gemini_free_tier_limits(monkeypatch):
    monkeypatch.setenv("GEMINI_FREE_RPM", "10")
    monkeypatch.setenv("GEMINI_FREE_TPM", "250000")
    monkeypatch.setenv("GEMINI_FREE_RPD", "500")


def _profile(**overrides) -> SearchProfile:
    defaults = dict(
        timezone="Europe/Berlin",
        scheduled_hour=9,
        max_jobs_per_run=35,
        source_minimum_per_run=0,
        source_max_share=0.5,
        thresholds={"package": 75, "possible": 65},
        salary_floor_eur=90000,
        target_titles=[],
        positive_keywords=[],
        blocked_title_keywords=[],
        search_queries=[],
        ats={"ashby": [], "lever": [], "greenhouse": []},
        max_search_queries_per_run=30,
        max_canonical_resolutions_per_run=80,
        max_learned_ats_boards_per_run=75,
        markets=[],
    )
    defaults.update(overrides)
    return SearchProfile(**defaults)


def _store_for(profile: SearchProfile) -> PostgresJobStore:
    store = PostgresJobStore(FakeSupabaseClient())
    store.save_search_profile(profile)
    return store


def _load(profile: SearchProfile):
    return load_settings(_store_for(profile))


def test_load_settings_reads_the_search_profile(monkeypatch):
    _set_required_bot_env(monkeypatch)
    profile = _profile(
        target_titles=["senior product engineer"],
        markets=[
            SearchProfileMarket(
                market_id="germany_eu",
                query_share=1.0,
                currency="EUR",
                gross_base_floor=90000,
                remote_policy="preferred",
                relocation_policy="selective",
                sponsorship_policy="not_required",
                direct_sources=["devjobs"],
            )
        ],
    )
    settings = _load(profile)
    assert settings.timezone == "Europe/Berlin"
    assert settings.policy.salary_floor_eur == 90000
    assert settings.policy.target_titles == ["senior product engineer"]
    assert len(settings.policy.markets) == 1
    assert settings.policy.markets[0].id == "germany_eu"
    assert settings.policy.markets[0].direct_sources == ["devjobs"]


def test_load_settings_raises_when_no_profile_exists():
    store = PostgresJobStore(FakeSupabaseClient())
    with pytest.raises(ProfileNotFoundError):
        load_settings(store)


def test_loaders_receive_identical_gemini_free_tier_quota(monkeypatch):
    _set_required_bot_env(monkeypatch)
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")

    bot_settings = _load(_profile())
    gmail_settings = load_gmail_settings()

    assert bot_settings.gemini_quota == gmail_settings.gemini_quota
    assert bot_settings.gemini_quota.rpm == 10
    assert bot_settings.gemini_quota.tpm == 250000
    assert bot_settings.gemini_quota.rpd == 500
    assert bot_settings.gemini_quota.ceiling_ratio == 0.80
    assert bot_settings.gemini_quota.core_reserve_ratio == 0.25
    assert bot_settings.gemini_quota.rate_pause_seconds == 90


@pytest.mark.parametrize(
    "name", ["GEMINI_FREE_RPM", "GEMINI_FREE_TPM", "GEMINI_FREE_RPD"]
)
def test_gemini_free_tier_limit_is_required(monkeypatch, name):
    _set_required_bot_env(monkeypatch)
    monkeypatch.delenv(name)

    with pytest.raises(ValueError, match=name):
        _load(_profile())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GEMINI_FREE_RPM", "0"),
        ("GEMINI_FREE_TPM", "-1"),
        ("GEMINI_FREE_RPD", "not-an-integer"),
    ],
)
def test_gemini_free_tier_limit_must_be_a_positive_integer(monkeypatch, name, value):
    _set_required_bot_env(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name} must be a positive integer"):
        _load(_profile())


def test_load_gmail_settings_does_not_require_candidate_profile(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    monkeypatch.delenv("CANDIDATE_PROFILE_B64", raising=False)
    settings = load_gmail_settings()
    assert settings.client_id == "client"


def test_load_gmail_settings_requires_refresh_token(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    monkeypatch.delenv("GMAIL_REFRESH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GMAIL_REFRESH_TOKEN"):
        load_gmail_settings()


def test_load_settings_decodes_private_sources(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    settings = _load(_profile(max_jobs_per_run=25))
    assert settings.candidate_profile == "profile"
    assert settings.cover_letter_template == "template"
    assert settings.timezone == "Europe/Berlin"
    assert settings.dry_run is True


def test_load_settings_dry_run_env_zero_is_false(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    settings = _load(_profile(max_jobs_per_run=25))
    assert settings.dry_run is False


def test_load_settings_requires_telegram_in_non_dry_run(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.delenv("JOB_HUNTER_DRY_RUN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises((ValueError, KeyError)):
        _load(_profile(max_jobs_per_run=25))


def test_load_settings_discovery_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    settings = _load(
        _profile(
            max_jobs_per_run=75,
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
            ],
            max_search_queries_per_run=4,
            role_families=[
                "staff product engineer",
                "senior software engineer frontend",
            ],
            search_query_templates=['"{role}" React TypeScript remote Europe'],
            search_domains=["jobs.ashbyhq.com"],
            specialist_search_domains=["wellfound.com", "app.welcometothejungle.com"],
            specialist_query_templates=['"{role}" remote Europe'],
            yc_job_pages=["https://www.ycombinator.com/jobs/role"],
            manual_company_watch=[
                {
                    "company_name": "Acme GmbH",
                    "ats_provider": "greenhouse",
                    "ats_identifier": "acme",
                },
                {"company_name": "Beta", "careers_url": "https://beta.test/careers"},
            ],
            search_queries=['"Senior Product Engineer" remote'],
        )
    )
    assert settings.policy.max_jobs_per_run == 75
    assert settings.policy.engineering_title_keywords == ["engineer", "developer"]
    assert settings.policy.engineering_title_phrases == [
        "technical lead",
        "frontend lead",
        "software architect",
    ]
    assert settings.policy.blocked_profession_title_phrases == [
        "product manager",
        "product designer",
        "sales engineer",
        "data engineer",
    ]
    assert settings.policy.max_search_queries_per_run == 4
    assert settings.policy.role_families == [
        "staff product engineer",
        "senior software engineer frontend",
    ]
    assert settings.policy.search_query_templates == [
        '"{role}" React TypeScript remote Europe'
    ]
    assert settings.policy.search_domains == ["jobs.ashbyhq.com"]
    assert settings.policy.specialist_search_domains == [
        "wellfound.com",
        "app.welcometothejungle.com",
    ]
    assert settings.policy.specialist_query_templates == ['"{role}" remote Europe']
    assert settings.policy.yc_job_pages == ["https://www.ycombinator.com/jobs/role"]
    assert settings.policy.manual_company_watch == [
        CompanyWatchSeed(
            company_name="Acme GmbH",
            ats_provider="greenhouse",
            ats_identifier="acme",
        ),
        CompanyWatchSeed(
            company_name="Beta",
            careers_url="https://beta.test/careers",
        ),
    ]
    assert settings.policy.search_queries == ['"Senior Product Engineer" remote']


def test_load_settings_uses_profile_discovery_defaults(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")

    settings = _load(_profile())

    assert settings.policy.max_jobs_per_run == 35
    assert settings.policy.source_minimum_per_run == 0
    assert settings.policy.source_max_share == 0.5
    assert settings.policy.manual_company_watch == []
    assert settings.policy.max_learned_ats_boards_per_run == 75
    assert settings.policy.learned_ats_denylist == []


def test_load_settings_reads_learned_ats_denylist(monkeypatch):
    _set_required_bot_env(monkeypatch)
    settings = _load(_profile(learned_ats_denylist=["lever:jobgether"]))
    assert settings.policy.learned_ats_denylist == ["lever:jobgether"]


def test_load_settings_normalizes_learned_ats_denylist_entries(monkeypatch):
    _set_required_bot_env(monkeypatch)
    settings = _load(_profile(learned_ats_denylist=[" Lever:JobGether "]))
    assert settings.policy.learned_ats_denylist == ["lever:jobgether"]


def test_load_settings_rejects_malformed_learned_ats_denylist_entry(monkeypatch):
    _set_required_bot_env(monkeypatch)
    with pytest.raises(ValueError, match="learned_ats_denylist"):
        _load(_profile(learned_ats_denylist=["jobgether"]))


def test_load_settings_defaults_learned_ats_allowlist_to_no_entries(monkeypatch):
    _set_required_bot_env(monkeypatch)
    settings = _load(_profile())
    assert settings.policy.learned_ats_allowlist == []


def test_load_settings_reads_and_normalizes_learned_ats_allowlist(monkeypatch):
    _set_required_bot_env(monkeypatch)
    settings = _load(_profile(learned_ats_allowlist=[" Lever:ClientCo "]))
    assert settings.policy.learned_ats_allowlist == ["lever:clientco"]


def test_load_settings_rejects_malformed_learned_ats_allowlist_entry(monkeypatch):
    _set_required_bot_env(monkeypatch)
    with pytest.raises(ValueError, match="learned_ats_allowlist"):
        _load(_profile(learned_ats_allowlist=["clientco"]))


def test_load_settings_rejects_a_board_in_both_ats_lists(monkeypatch):
    # The two lists express opposite operator intent; honouring either one
    # silently would hide an editing mistake in the only operator surface.
    _set_required_bot_env(monkeypatch)
    with pytest.raises(ValueError, match="lever:clientco"):
        _load(
            _profile(
                learned_ats_denylist=["lever:clientco"],
                learned_ats_allowlist=["Lever:ClientCo"],
            )
        )


def test_load_settings_supports_legacy_manual_company_names():
    # _parse_manual_company_watch still accepts bare-string entries (a
    # SearchProfile-backed profile now always stores dicts, but the parsing
    # function itself is unchanged and reused as-is -- exercise it directly).
    assert _parse_manual_company_watch(["Acme GmbH"]) == [
        CompanyWatchSeed(company_name="Acme GmbH")
    ]


@pytest.mark.parametrize(
    "value",
    ["Acme", {"company_name": "Acme"}],
    ids=["scalar", "mapping"],
)
def test_load_settings_rejects_non_list_manual_company_watch(value):
    with pytest.raises(ValueError, match="manual_company_watch must be a list"):
        _parse_manual_company_watch(value)


@pytest.mark.parametrize(
    "entry",
    ["   ", {}, {"company_name": 123}],
    ids=["empty-string", "missing-company-name", "non-string-company-name"],
)
def test_load_settings_rejects_invalid_manual_company_name(entry):
    with pytest.raises(
        ValueError,
        match=r"manual_company_watch\[0\].*company_name.*non-empty string",
    ):
        _parse_manual_company_watch([entry])


@pytest.mark.parametrize(
    "field,value",
    [
        ("careers_url", 123),
        ("ats_provider", ["greenhouse"]),
        ("ats_identifier", {"board": "acme"}),
    ],
)
def test_load_settings_rejects_non_string_manual_watch_optional_field(
    monkeypatch, field, value
):
    _set_required_bot_env(monkeypatch)
    with pytest.raises(
        ValueError,
        match=rf"manual_company_watch\[0\].{field} must be a string or null",
    ):
        _load(
            _profile(
                manual_company_watch=[{"company_name": "Acme", field: value}]
            )
        )


def test_load_settings_rejects_unknown_manual_watch_mapping_key(monkeypatch):
    _set_required_bot_env(monkeypatch)
    with pytest.raises(
        ValueError,
        match=r"manual_company_watch\[0\]\.ats_identifer is not allowed",
    ):
        _load(
            _profile(
                manual_company_watch=[
                    {"company_name": "Acme", "ats_identifer": "acme"}
                ]
            )
        )


def test_load_settings_parses_markets_in_declared_order(monkeypatch):
    _set_required_bot_env(monkeypatch)
    settings = _load(
        _profile(
            markets=[
                SearchProfileMarket(
                    market_id="germany_eu",
                    query_share=0.35,
                    locations=["Berlin", "Germany", "Europe"],
                    allowed_languages=["English"],
                    currency="EUR",
                    gross_base_floor=90000,
                    remote_policy="preferred",
                    relocation_policy="selective",
                    sponsorship_policy="not_required",
                ),
                SearchProfileMarket(
                    market_id="israel_remote",
                    query_share=0.25,
                    locations=["Israel", "Tel Aviv"],
                    allowed_languages=["English", "Hebrew"],
                    currency="ILS",
                    gross_base_floor=420000,
                    remote_policy="required",
                    relocation_policy="none",
                    sponsorship_policy="not_required",
                ),
            ]
        )
    )

    assert [market.id for market in settings.policy.markets] == [
        "germany_eu",
        "israel_remote",
    ]
    assert settings.policy.markets[1].allowed_languages == ["English", "Hebrew"]
    assert settings.policy.markets[1].salary.gross_base_floor == 420000


def test_load_settings_rejects_duplicate_market_ids(monkeypatch):
    _set_required_bot_env(monkeypatch)
    market_kwargs = dict(
        market_id="london",
        query_share=0.5,
        locations=["London"],
        allowed_languages=["English"],
        currency="GBP",
        gross_base_floor=90000,
        remote_policy="allowed",
        relocation_policy="allowed",
        sponsorship_policy="required",
    )
    with pytest.raises(ValueError, match="duplicate market id: london"):
        _load(
            _profile(
                markets=[
                    SearchProfileMarket(**market_kwargs),
                    SearchProfileMarket(**market_kwargs),
                ]
            )
        )


def test_market_source_config_distinguishes_direct_and_discovery(monkeypatch):
    _set_required_bot_env(monkeypatch)
    settings = _load(
        _profile(
            markets=[
                SearchProfileMarket(
                    market_id="israel_remote",
                    query_share=1.0,
                    locations=["Israel"],
                    allowed_languages=["English", "Hebrew"],
                    currency="ILS",
                    gross_base_floor=420000,
                    remote_policy="required",
                    relocation_policy="none",
                    sponsorship_policy="not_required",
                    direct_sources=["devjobs"],
                    discovery_domains=["jobs.techaviv.com", "jobs.ashbyhq.com"],
                )
            ]
        )
    )

    market = settings.policy.markets[0]
    assert market.direct_sources == ["devjobs"]
    assert market.discovery_domains == ["jobs.techaviv.com", "jobs.ashbyhq.com"]


def test_load_settings_rejects_non_positive_location_floor(monkeypatch):
    # SearchProfileMarket.location_floors is `dict[str, int]` with no
    # per-value constraint at the Pydantic layer, so a non-positive floor
    # still reaches config.py's unchanged _parse_markets check and must
    # still raise there.
    _set_required_bot_env(monkeypatch)
    with pytest.raises(
        ValueError, match=r"salary\.location_floors\.Berlin must be positive"
    ):
        _load(
            _profile(
                markets=[
                    SearchProfileMarket(
                        market_id="germany_eu",
                        query_share=0.5,
                        locations=["Berlin"],
                        allowed_languages=["English"],
                        currency="EUR",
                        gross_base_floor=90000,
                        location_floors={"Berlin": 0},
                        remote_policy="preferred",
                        relocation_policy="selective",
                        sponsorship_policy="not_required",
                    )
                ]
            )
        )

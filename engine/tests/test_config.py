import pytest

from engine.config import (
    ProfileNotFoundError,
    RuntimeConfigurationError,
    _parse_manual_company_watch,
    load_settings,
)
from engine.models import (
    DEFAULT_BACKEND_HEAVY_SIGNALS,
    DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES,
    DEFAULT_ENGINEERING_TITLE_KEYWORDS,
    DEFAULT_ENGINEERING_TITLE_PHRASES,
    DEFAULT_FRONTEND_SIGNALS,
    DEFAULT_SPECIALIST_BOARD_HOSTS,
    CompanyWatchSeed,
    ProviderCredentials,
)
from engine.postgres_store import PostgresJobStore
from engine.search_profile import SearchProfile, SearchProfileMarket
from tests.fake_supabase_client import FakeSupabaseClient


def _set_runtime_env(monkeypatch):
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


class _FakeRuntimeStore:
    def __init__(
        self,
        profile: SearchProfile,
        *,
        credentials: ProviderCredentials | None = None,
        documents: dict[str, str] | None = None,
    ) -> None:
        self._profile_store = PostgresJobStore(FakeSupabaseClient())
        self._profile_store.save_search_profile(profile)
        self.credentials = credentials or ProviderCredentials(
            gemini_api_key="stored-gemini",
            brave_search_api_key="stored-brave",
        )
        self.documents = (
            {"cv": "stored profile", "cover_letter": "stored template"}
            if documents is None
            else documents
        )
        self.document_reads = 0

    def get_search_profile(self):
        return self._profile_store.get_search_profile()

    def get_provider_credentials(self) -> ProviderCredentials:
        return self.credentials

    def get_source_documents(self) -> dict[str, str]:
        self.document_reads += 1
        return self.documents


def _store_for(
    profile: SearchProfile,
    *,
    credentials: ProviderCredentials | None = None,
    documents: dict[str, str] | None = None,
) -> _FakeRuntimeStore:
    return _FakeRuntimeStore(
        profile,
        credentials=credentials,
        documents=documents,
    )


def _load(profile: SearchProfile, **store_kwargs):
    return load_settings(_store_for(profile, **store_kwargs))


def test_load_settings_reads_the_search_profile(monkeypatch):
    _set_runtime_env(monkeypatch)
    profile = _profile(
        target_titles=["senior product engineer"],
        specialist_board_hosts=["jobs.lever.co"],
        frontend_signals=["react", "typescript"],
        backend_heavy_signals=["kubernetes", "kafka"],
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
    # Regression: these three lists must round-trip from the saved profile --
    # they were previously dropped when constructing SearchPolicy, silently
    # giving every user the dataclass default [] regardless of what they saved.
    assert settings.policy.specialist_board_hosts == ["jobs.lever.co"]
    assert settings.policy.frontend_signals == ["react", "typescript"]
    assert settings.policy.backend_heavy_signals == ["kubernetes", "kafka"]


def test_load_settings_raises_when_no_profile_exists():
    store = PostgresJobStore(FakeSupabaseClient())
    with pytest.raises(ProfileNotFoundError):
        load_settings(store)


def test_load_settings_reads_documents_and_provider_keys_from_store(monkeypatch):
    _set_runtime_env(monkeypatch)

    settings = _load(_profile())

    assert settings.ai_api_key == "stored-gemini"
    assert settings.brave_search_api_key == "stored-brave"
    assert settings.candidate_profile == "stored profile"
    assert settings.cover_letter_template == "stored template"


def test_load_settings_does_not_read_four_legacy_environment_variables(monkeypatch):
    _set_runtime_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-gemini-sentinel")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "legacy-brave-sentinel")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", "legacy-cv-sentinel")
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", "legacy-cover-letter-sentinel")

    settings = _load(_profile())

    assert settings.ai_api_key == "stored-gemini"
    assert settings.brave_search_api_key == "stored-brave"
    assert settings.candidate_profile == "stored profile"
    assert settings.cover_letter_template == "stored template"


def test_load_settings_names_missing_gemini_cv_and_cover_letter_without_values(
    monkeypatch,
):
    _set_runtime_env(monkeypatch)
    store = _store_for(
        _profile(),
        credentials=ProviderCredentials(brave_search_api_key="brave-only"),
        documents={},
    )

    with pytest.raises(RuntimeConfigurationError) as exc_info:
        load_settings(store)

    assert str(exc_info.value) == (
        "Missing per-user Job Hunter configuration: gemini, cv, cover_letter"
    )


def test_load_settings_accepts_missing_brave_key(monkeypatch):
    _set_runtime_env(monkeypatch)

    settings = _load(
        _profile(),
        credentials=ProviderCredentials(gemini_api_key="stored-gemini"),
    )

    assert settings.brave_search_api_key is None


def test_load_settings_reads_the_gemini_free_tier_quota(monkeypatch):
    _set_runtime_env(monkeypatch)

    settings = _load(_profile())

    assert settings.ai_quota.rpm == 10
    assert settings.ai_quota.tpm == 250000
    assert settings.ai_quota.rpd == 500
    assert settings.ai_quota.ceiling_ratio == 0.80
    assert settings.ai_quota.core_reserve_ratio == 0.25
    assert settings.ai_quota.rate_pause_seconds == 90


def test_a_run_starts_with_an_api_key_alone_and_takes_the_published_limits(
    monkeypatch,
):
    """A user supplies a key; the free-tier limits are defaults in code (#73)."""
    _set_runtime_env(monkeypatch)
    for name in ("GEMINI_FREE_RPM", "GEMINI_FREE_TPM", "GEMINI_FREE_RPD"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

    settings = _load(_profile())

    assert (settings.ai_quota.rpm, settings.ai_quota.tpm, settings.ai_quota.rpd) == (
        15,
        250_000,
        500,
    )


@pytest.mark.parametrize(
    ("name", "attribute", "value"),
    [
        ("GEMINI_FREE_RPM", "rpm", 3),
        ("GEMINI_FREE_TPM", "tpm", 111_000),
        ("GEMINI_FREE_RPD", "rpd", 7),
    ],
)
def test_each_free_tier_variable_overrides_only_its_own_default(
    monkeypatch, name, attribute, value
):
    _set_runtime_env(monkeypatch)
    for other in ("GEMINI_FREE_RPM", "GEMINI_FREE_TPM", "GEMINI_FREE_RPD"):
        monkeypatch.delenv(other)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv(name, str(value))

    quota = _load(_profile()).ai_quota

    published = {"rpm": 15, "tpm": 250_000, "rpd": 500}
    published[attribute] = value
    assert (quota.rpm, quota.tpm, quota.rpd) == (
        published["rpm"],
        published["tpm"],
        published["rpd"],
    )


def test_an_empty_model_variable_is_treated_as_unset(monkeypatch):
    """`GEMINI_MODEL: ${{ vars.GEMINI_MODEL }}` expands to "" when unset."""
    _set_runtime_env(monkeypatch)
    monkeypatch.setenv("GEMINI_MODEL", "")

    settings = _load(_profile())

    assert settings.ai_model == "gemini-3.5-flash-lite"


def test_a_model_absent_from_the_defaults_table_runs_conservatively_and_says_so(
    monkeypatch, caplog
):
    _set_runtime_env(monkeypatch)
    for name in ("GEMINI_FREE_RPM", "GEMINI_FREE_TPM", "GEMINI_FREE_RPD"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-9.9-imaginary")

    with caplog.at_level("WARNING"):
        quota = _load(_profile()).ai_quota

    assert (quota.rpm, quota.rpd) == (5, 100)
    assert "gemini-9.9-imaginary" in caplog.text


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GEMINI_FREE_RPM", "0"),
        ("GEMINI_FREE_TPM", "-1"),
        ("GEMINI_FREE_RPD", "not-an-integer"),
    ],
)
def test_a_free_tier_override_that_was_typed_on_purpose_must_be_usable(
    monkeypatch, name, value
):
    _set_runtime_env(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name} must be a positive integer"):
        _load(_profile())


def test_load_settings_reads_private_sources(monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")

    settings = _load(_profile(max_jobs_per_run=25))
    assert settings.candidate_profile == "stored profile"
    assert settings.cover_letter_template == "stored template"
    assert settings.timezone == "Europe/Berlin"
    assert settings.dry_run is True


def test_load_settings_dry_run_env_zero_is_false(monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "0")

    settings = _load(_profile(max_jobs_per_run=25))
    assert settings.dry_run is False


def test_load_settings_discovery_config(monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")

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
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")

    settings = _load(_profile())

    assert settings.policy.max_jobs_per_run == 35
    assert settings.policy.daily_offer_limit == 10
    assert settings.policy.match_score_floor == 80
    assert settings.policy.source_minimum_per_run == 0
    assert settings.policy.source_max_share == 0.5
    assert settings.policy.manual_company_watch == []
    assert settings.policy.max_learned_ats_boards_per_run == 75
    assert settings.policy.learned_ats_denylist == []


@pytest.mark.parametrize("limit", [5, 10, 20])
def test_load_settings_reads_the_daily_offer_limit(monkeypatch, limit):
    _set_runtime_env(monkeypatch)

    settings = _load(_profile(daily_offer_limit=limit))

    assert settings.policy.daily_offer_limit == limit


def test_load_settings_reads_the_match_score_floor(monkeypatch):
    _set_runtime_env(monkeypatch)

    settings = _load(_profile(match_score_floor=70))

    assert settings.policy.match_score_floor == 70


def test_load_settings_falls_back_to_defaults_for_empty_ranking_lists(monkeypatch):
    """A profile that never explicitly configured these six list fields still
    gets the known-good ranking defaults, not an empty list.

    Every one of these columns is `not null default '{}'` in Postgres, so
    "missing" never happens -- but "empty" does, for any profile that was
    never edited for these fields. Detection logic must work with zero
    operator knowledge, so an unconfigured profile must fall back exactly
    like one saved before these fields existed.
    """
    _set_runtime_env(monkeypatch)
    settings = _load(
        _profile(
            engineering_title_keywords=[],
            engineering_title_phrases=[],
            blocked_profession_title_phrases=[],
            specialist_board_hosts=[],
            frontend_signals=[],
            backend_heavy_signals=[],
        )
    )
    assert settings.policy.engineering_title_keywords == DEFAULT_ENGINEERING_TITLE_KEYWORDS
    assert settings.policy.engineering_title_phrases == DEFAULT_ENGINEERING_TITLE_PHRASES
    assert (
        settings.policy.blocked_profession_title_phrases
        == DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES
    )
    assert settings.policy.specialist_board_hosts == DEFAULT_SPECIALIST_BOARD_HOSTS
    assert settings.policy.frontend_signals == DEFAULT_FRONTEND_SIGNALS
    assert settings.policy.backend_heavy_signals == DEFAULT_BACKEND_HEAVY_SIGNALS


def test_load_settings_explicit_ranking_lists_override_rather_than_merge(monkeypatch):
    """A non-empty configured value replaces the default outright -- it is
    not merged with it."""
    _set_runtime_env(monkeypatch)
    settings = _load(
        _profile(
            engineering_title_keywords=["custom-keyword"],
            engineering_title_phrases=["custom-phrase"],
            blocked_profession_title_phrases=["custom-blocked"],
            specialist_board_hosts=["custom.example.com"],
            frontend_signals=["custom-frontend"],
            backend_heavy_signals=["custom-backend"],
        )
    )
    assert settings.policy.engineering_title_keywords == ["custom-keyword"]
    assert settings.policy.engineering_title_phrases == ["custom-phrase"]
    assert settings.policy.blocked_profession_title_phrases == ["custom-blocked"]
    assert settings.policy.specialist_board_hosts == ["custom.example.com"]
    assert settings.policy.frontend_signals == ["custom-frontend"]
    assert settings.policy.backend_heavy_signals == ["custom-backend"]


def test_load_settings_reads_learned_ats_denylist(monkeypatch):
    _set_runtime_env(monkeypatch)
    settings = _load(_profile(learned_ats_denylist=["lever:jobgether"]))
    assert settings.policy.learned_ats_denylist == ["lever:jobgether"]


def test_load_settings_normalizes_learned_ats_denylist_entries(monkeypatch):
    _set_runtime_env(monkeypatch)
    settings = _load(_profile(learned_ats_denylist=[" Lever:JobGether "]))
    assert settings.policy.learned_ats_denylist == ["lever:jobgether"]


def test_load_settings_rejects_malformed_learned_ats_denylist_entry(monkeypatch):
    _set_runtime_env(monkeypatch)
    with pytest.raises(ValueError, match="learned_ats_denylist"):
        _load(_profile(learned_ats_denylist=["jobgether"]))


def test_load_settings_defaults_learned_ats_allowlist_to_no_entries(monkeypatch):
    _set_runtime_env(monkeypatch)
    settings = _load(_profile())
    assert settings.policy.learned_ats_allowlist == []


def test_load_settings_reads_and_normalizes_learned_ats_allowlist(monkeypatch):
    _set_runtime_env(monkeypatch)
    settings = _load(_profile(learned_ats_allowlist=[" Lever:ClientCo "]))
    assert settings.policy.learned_ats_allowlist == ["lever:clientco"]


def test_load_settings_rejects_malformed_learned_ats_allowlist_entry(monkeypatch):
    _set_runtime_env(monkeypatch)
    with pytest.raises(ValueError, match="learned_ats_allowlist"):
        _load(_profile(learned_ats_allowlist=["clientco"]))


def test_load_settings_rejects_a_board_in_both_ats_lists(monkeypatch):
    # The two lists express opposite operator intent; honouring either one
    # silently would hide an editing mistake in the only operator surface.
    _set_runtime_env(monkeypatch)
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
    _set_runtime_env(monkeypatch)
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
    _set_runtime_env(monkeypatch)
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
    _set_runtime_env(monkeypatch)
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
    _set_runtime_env(monkeypatch)
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
    _set_runtime_env(monkeypatch)
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
    _set_runtime_env(monkeypatch)
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


def test_source_time_budget_defaults_generously(monkeypatch):
    """An unconfigured budget must not truncate healthy sources.

    The whole point of the default is that the first run after the budget
    lands still measures the sources rather than the budget: the reference
    run's discovery took 47 minutes across its sources, so a default below
    that would report the cap instead of the cost.
    """
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.delenv("JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS", raising=False)

    settings = _load(_profile(max_jobs_per_run=25))

    assert settings.policy.source_time_budget_seconds >= 1800.0


def test_source_time_budget_is_overridable_from_the_environment(monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.setenv("JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS", "120")

    settings = _load(_profile(max_jobs_per_run=25))

    assert settings.policy.source_time_budget_seconds == 120.0


@pytest.mark.parametrize("raw", ["nonsense", "0", "-30", "nan", "inf", "1e400"])
def test_an_unusable_source_time_budget_falls_back_to_the_default(monkeypatch, raw):
    """A bad value must not become a zero-second budget or no budget at all.

    Reading "0" or a typo as the budget would cut every source off at its
    first unit -- a misconfiguration turning a safeguard into the outage it
    exists to prevent. The non-finite floats `float()` accepts fail the other
    way: `nan` compares False against every elapsed time, so it would disable
    the budget for the whole run while looking configured.
    """
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.setenv("JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS", raw)

    settings = _load(_profile(max_jobs_per_run=25))

    assert settings.policy.source_time_budget_seconds == 1800.0

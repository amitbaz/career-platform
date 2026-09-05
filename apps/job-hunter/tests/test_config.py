import base64
from pathlib import Path
import pytest

from job_hunter.config import load_settings
from job_hunter.config import load_gmail_settings
from job_hunter.models import CompanyWatchSeed


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


def _load_manual_watch_config(monkeypatch, tmp_path, manual_watch_yaml):
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds:\n  package: 75\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        f"manual_company_watch: {manual_watch_yaml}\n"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv(
        "COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode()
    )
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    return load_settings(cfg)


def test_loaders_receive_identical_gemini_free_tier_quota(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds:\n  package: 75\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
    )
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")

    bot_settings = load_settings(cfg)
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
def test_gemini_free_tier_limit_is_required(monkeypatch, tmp_path, name):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds:\n  package: 75\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
    )
    monkeypatch.delenv(name)

    with pytest.raises(ValueError, match=name):
        load_settings(cfg)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GEMINI_FREE_RPM", "0"),
        ("GEMINI_FREE_TPM", "-1"),
        ("GEMINI_FREE_RPD", "not-an-integer"),
    ],
)
def test_gemini_free_tier_limit_must_be_a_positive_integer(
    monkeypatch, tmp_path, name, value
):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds:\n  package: 75\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
    )
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name} must be a positive integer"):
        load_settings(cfg)


def test_load_gmail_settings_does_not_require_candidate_profile(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    monkeypatch.delenv("CANDIDATE_PROFILE_B64", raising=False)
    settings = load_gmail_settings()
    assert settings.client_id == "client"
    assert settings.db_path == "var/job_hunter.sqlite3"


def test_load_gmail_settings_requires_refresh_token(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    monkeypatch.delenv("GMAIL_REFRESH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GMAIL_REFRESH_TOKEN"):
        load_gmail_settings()


def test_load_settings_decodes_private_sources(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "timezone: Europe/Berlin\nscheduled_hour: 9\nmax_jobs_per_run: 25\n"
        "thresholds:\n  package: 75\n  possible: 65\nsalary_floor_eur: 90000\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "search_queries: []\nats:\n  ashby: []\n  lever: []\n  greenhouse: []\n"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    settings = load_settings(cfg)
    assert settings.candidate_profile == "profile"
    assert settings.cover_letter_template == "template"
    assert settings.timezone == "Europe/Berlin"
    assert settings.dry_run is True


def test_load_settings_dry_run_env_zero_is_false(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "timezone: Europe/Berlin\nscheduled_hour: 9\nmax_jobs_per_run: 25\n"
        "thresholds:\n  package: 75\n  possible: 65\nsalary_floor_eur: 90000\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "search_queries: []\nats:\n  ashby: []\n  lever: []\n  greenhouse: []\n"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    settings = load_settings(cfg)
    assert settings.dry_run is False


def test_load_settings_requires_telegram_in_non_dry_run(monkeypatch, tmp_path: Path):
    import pytest
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "timezone: Europe/Berlin\nscheduled_hour: 9\nmax_jobs_per_run: 25\n"
        "thresholds:\n  package: 75\n  possible: 65\nsalary_floor_eur: 90000\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "search_queries: []\nats:\n  ashby: []\n  lever: []\n  greenhouse: []\n"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.delenv("JOB_HUNTER_DRY_RUN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises((ValueError, KeyError)):
        load_settings(cfg)


def test_load_settings_discovery_config(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "timezone: Europe/Berlin\nscheduled_hour: 9\nmax_jobs_per_run: 75\n"
        "thresholds:\n  package: 75\n  possible: 65\nsalary_floor_eur: 90000\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "engineering_title_keywords:\n"
        "  - engineer\n"
        "  - developer\n"
        "engineering_title_phrases:\n"
        "  - technical lead\n"
        "  - frontend lead\n"
        "  - software architect\n"
        "blocked_profession_title_phrases:\n"
        "  - product manager\n"
        "  - product designer\n"
        "  - sales engineer\n"
        "  - data engineer\n"
        "max_search_queries_per_run: 4\n"
        "role_families:\n"
        "  - staff product engineer\n"
        "  - senior software engineer frontend\n"
        "search_query_templates:\n"
        "  - '\"{role}\" React TypeScript remote Europe'\n"
        "search_domains:\n"
        "  - jobs.ashbyhq.com\n"
        "specialist_search_domains:\n"
        "  - wellfound.com\n"
        "  - app.welcometothejungle.com\n"
        "specialist_query_templates:\n"
        "  - '\"{role}\" remote Europe'\n"
        "yc_job_pages:\n"
        "  - https://www.ycombinator.com/jobs/role\n"
        "manual_company_watch:\n"
        "  - company_name: Acme GmbH\n"
        "    ats_provider: greenhouse\n"
        "    ats_identifier: acme\n"
        "  - company_name: Beta\n"
        "    careers_url: https://beta.test/careers\n"
        "search_queries:\n"
        "  - '\"Senior Product Engineer\" remote'\n"
        "ats:\n  ashby: []\n  lever: []\n  greenhouse: []\n"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    settings = load_settings(cfg)
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


def test_load_settings_uses_profile_discovery_defaults(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "timezone: Europe/Berlin\nscheduled_hour: 9\n"
        "thresholds:\n  package: 75\n  possible: 65\nsalary_floor_eur: 90000\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "search_queries: []\nats:\n  ashby: []\n  lever: []\n  greenhouse: []\n"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    settings = load_settings(cfg)

    assert settings.policy.max_jobs_per_run == 35
    assert settings.policy.source_minimum_per_run == 0
    assert settings.policy.source_max_share == 0.5
    assert settings.policy.manual_company_watch == []
    assert settings.policy.max_learned_ats_boards_per_run == 75


def test_load_settings_supports_legacy_manual_company_names(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds:\n  package: 75\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "manual_company_watch:\n  - Acme GmbH\n"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv(
        "COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode()
    )
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")

    settings = load_settings(cfg)

    assert settings.policy.manual_company_watch == [
        CompanyWatchSeed(company_name="Acme GmbH")
    ]


@pytest.mark.parametrize(
    "manual_watch_yaml",
    ["Acme", "{company_name: Acme}"],
    ids=["scalar", "mapping"],
)
def test_load_settings_rejects_non_list_manual_company_watch(
    monkeypatch, tmp_path, manual_watch_yaml
):
    with pytest.raises(ValueError, match="manual_company_watch must be a list"):
        _load_manual_watch_config(monkeypatch, tmp_path, manual_watch_yaml)


@pytest.mark.parametrize(
    "entry_yaml",
    ["'   '", "{}", "{company_name: 123}"],
    ids=["empty-string", "missing-company-name", "non-string-company-name"],
)
def test_load_settings_rejects_invalid_manual_company_name(
    monkeypatch, tmp_path, entry_yaml
):
    with pytest.raises(
        ValueError,
        match=r"manual_company_watch\[0\].*company_name.*non-empty string",
    ):
        _load_manual_watch_config(monkeypatch, tmp_path, f"[{entry_yaml}]")


@pytest.mark.parametrize(
    "field,value",
    [
        ("careers_url", "123"),
        ("ats_provider", "[greenhouse]"),
        ("ats_identifier", "{board: acme}"),
    ],
)
def test_load_settings_rejects_non_string_manual_watch_optional_field(
    monkeypatch, tmp_path, field, value
):
    with pytest.raises(
        ValueError,
        match=rf"manual_company_watch\[0\].{field} must be a string or null",
    ):
        _load_manual_watch_config(
            monkeypatch,
            tmp_path,
            f"[{{company_name: Acme, {field}: {value}}}]",
        )


def test_load_settings_rejects_unknown_manual_watch_mapping_key(
    monkeypatch, tmp_path
):
    with pytest.raises(
        ValueError,
        match=r"manual_company_watch\[0\]\.ats_identifer is not allowed",
    ):
        _load_manual_watch_config(
            monkeypatch,
            tmp_path,
            "[{company_name: Acme, ats_identifer: acme}]",
        )


def test_load_settings_parses_markets_in_declared_order(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - id: germany_eu\n"
        "    query_share: 0.35\n"
        "    locations: [Berlin, Germany, Europe]\n"
        "    allowed_languages: [English]\n"
        "    salary: {currency: EUR, gross_base_floor: 90000}\n"
        "    remote_policy: preferred\n"
        "    relocation_policy: selective\n"
        "    sponsorship_policy: not_required\n"
        "  - id: israel_remote\n"
        "    query_share: 0.25\n"
        "    locations: [Israel, Tel Aviv]\n"
        "    allowed_languages: [English, Hebrew]\n"
        "    salary: {currency: ILS, gross_base_floor: 420000}\n"
        "    remote_policy: required\n"
        "    relocation_policy: none\n"
        "    sponsorship_policy: not_required\n"
    )

    settings = load_settings(cfg)

    assert [market.id for market in settings.policy.markets] == [
        "germany_eu",
        "israel_remote",
    ]
    assert settings.policy.markets[1].allowed_languages == ["English", "Hebrew"]
    assert settings.policy.markets[1].salary.gross_base_floor == 420000


def test_load_settings_rejects_duplicate_market_ids(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - {id: london, query_share: 0.5, locations: [London], allowed_languages: [English], salary: {currency: GBP, gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: required}\n"
        "  - {id: london, query_share: 0.5, locations: [London], allowed_languages: [English], salary: {currency: GBP, gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: required}\n"
    )

    with pytest.raises(ValueError, match="duplicate market id: london"):
        load_settings(cfg)


@pytest.mark.parametrize(
    "bad_market_yaml,expected_error",
    [
        (
            "{id: a, query_share: -0.1, locations: [A], allowed_languages: [English], salary: {currency: EUR, gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: not_required}",
            "query_share cannot be negative",
        ),
        (
            "{id: a, query_share: 0.5, locations: [A], allowed_languages: [English], salary: {currency: '', gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: not_required}",
            "currency cannot be empty",
        ),
        (
            "{id: a, query_share: 0.5, locations: [A], allowed_languages: [English], salary: {currency: EUR, gross_base_floor: 0}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: not_required}",
            "salary.gross_base_floor must be positive",
        ),
        (
            "{id: a, query_share: 0.5, locations: [A], allowed_languages: [English], salary: {currency: EUR, gross_base_floor: 90000, location_floors: {Berlin: 0}}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: not_required}",
            "location_floors.*must be positive",
        ),
        (
            "{id: a, query_share: 0.5, locations: [A], allowed_languages: [English], salary: {currency: EUR, gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: not_required, unknown_field: 1}",
            "unknown_field is not allowed",
        ),
        (
            "{id: a, query_share: 0.5, locations: [A], allowed_languages: [English], salary: {currency: EUR, gross_base_floor: 90000}, remote_policy: invalid, relocation_policy: allowed, sponsorship_policy: not_required}",
            "invalid remote_policy: invalid",
        ),
        (
            "{id: a, query_share: 0.5, locations: [A], allowed_languages: [English], salary: {currency: EUR, gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: invalid, sponsorship_policy: not_required}",
            "invalid relocation_policy: invalid",
        ),
        (
            "{id: a, query_share: 0.5, locations: [A], allowed_languages: [English], salary: {currency: EUR, gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: invalid}",
            "invalid sponsorship_policy: invalid",
        ),
    ],
)
def test_load_settings_rejects_invalid_market(monkeypatch, tmp_path, bad_market_yaml, expected_error):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        f"  - {bad_market_yaml}\n"
    )
    with pytest.raises(ValueError, match=expected_error):
        load_settings(cfg)


def test_market_source_config_distinguishes_direct_and_discovery(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - id: israel_remote\n"
        "    query_share: 1.0\n"
        "    locations: [Israel]\n"
        "    allowed_languages: [English, Hebrew]\n"
        "    salary: {currency: ILS, gross_base_floor: 420000}\n"
        "    remote_policy: required\n"
        "    relocation_policy: none\n"
        "    sponsorship_policy: not_required\n"
        "    direct_sources: [devjobs]\n"
        "    discovery_domains: [jobs.techaviv.com, jobs.ashbyhq.com]\n"
    )

    market = load_settings(cfg).policy.markets[0]

    assert market.direct_sources == ["devjobs"]
    assert market.discovery_domains == ["jobs.techaviv.com", "jobs.ashbyhq.com"]


def test_legacy_source_domains_are_discovery_only(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - id: london\n"
        "    query_share: 1.0\n"
        "    locations: [London]\n"
        "    allowed_languages: [English]\n"
        "    salary: {currency: GBP, gross_base_floor: 90000}\n"
        "    remote_policy: allowed\n"
        "    relocation_policy: allowed\n"
        "    sponsorship_policy: required\n"
        "    source_domains: [wellfound.com]\n"
    )

    market = load_settings(cfg).policy.markets[0]

    assert market.direct_sources == []
    assert market.discovery_domains == ["wellfound.com"]


def test_market_rejects_source_domains_and_discovery_domains_together(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - id: london\n"
        "    query_share: 1.0\n"
        "    locations: [London]\n"
        "    allowed_languages: [English]\n"
        "    salary: {currency: GBP, gross_base_floor: 90000}\n"
        "    remote_policy: allowed\n"
        "    relocation_policy: allowed\n"
        "    sponsorship_policy: required\n"
        "    source_domains: [wellfound.com]\n"
        "    discovery_domains: [jobs.ashbyhq.com]\n"
    )

    with pytest.raises(ValueError, match="cannot define both source_domains and discovery_domains"):
        load_settings(cfg)

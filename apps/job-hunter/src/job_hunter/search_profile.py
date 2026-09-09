from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from job_hunter.company_facets import (
    VALID_BUSINESS_MODELS,
    VALID_INDUSTRIES,
    VALID_SIZE_BANDS,
    VALID_STAGES,
)

_REMOTE_POLICIES = {"preferred", "required", "allowed"}
_RELOCATION_POLICIES = {"none", "selective", "allowed"}
_SPONSORSHIP_POLICIES = {"not_required", "required"}

# Delivery policy. The daily offer limit is a closed product choice rather than
# a tuning knob, so it is a set and not a range. The match-score floor is a
# range, bounded away from both ends of the score scale on purpose: a floor of 0
# would disable the feature without saying so, and a floor at or near 100 would
# silence the digest permanently. Below the `possible` decision threshold the
# floor stops filtering anything the decision ladder does not already drop.
DailyOfferLimit = Literal[5, 10, 20]

#: Which company-preference list is checked against which vocabulary (#198).
#: "unknown" is excluded from all six on purpose. It is a member of every
#: extraction vocabulary because a company nobody has read has to be
#: representable, but it must never become something a user can express a
#: preference over: "exclude unknown industries" would suppress exactly the
#: companies the engine has not got to yet, turning a gap in the data into a
#: lost opportunity, which is the one outcome this feature must not produce.
_COMPANY_PREFERENCE_VOCABULARIES: dict[str, frozenset[str]] = {
    "preferred_industries": VALID_INDUSTRIES - {"unknown"},
    "excluded_industries": VALID_INDUSTRIES - {"unknown"},
    "preferred_business_models": VALID_BUSINESS_MODELS - {"unknown"},
    "excluded_business_models": VALID_BUSINESS_MODELS - {"unknown"},
    "preferred_company_stages": VALID_STAGES - {"unknown"},
    "preferred_company_sizes": VALID_SIZE_BANDS - {"unknown"},
}
_MATCH_SCORE_FLOOR_MIN = 50
_MATCH_SCORE_FLOOR_MAX = 95


class SearchProfileMarket(BaseModel):
    market_id: str = Field(min_length=1)
    query_share: float = Field(ge=0, le=1)
    locations: list[str] = Field(default_factory=list)
    allowed_languages: list[str] = Field(default_factory=list)
    currency: str = Field(min_length=1)
    gross_base_floor: int = Field(gt=0)
    location_floors: dict[str, int] = Field(default_factory=dict)
    remote_policy: str
    relocation_policy: str
    sponsorship_policy: str
    direct_sources: list[str] = Field(default_factory=list)
    discovery_domains: list[str] = Field(default_factory=list)
    query_templates: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    enabled: bool = True

    def model_post_init(self, __context) -> None:
        if self.remote_policy not in _REMOTE_POLICIES:
            raise ValueError(f"invalid remote_policy: {self.remote_policy!r}")
        if self.relocation_policy not in _RELOCATION_POLICIES:
            raise ValueError(f"invalid relocation_policy: {self.relocation_policy!r}")
        if self.sponsorship_policy not in _SPONSORSHIP_POLICIES:
            raise ValueError(f"invalid sponsorship_policy: {self.sponsorship_policy!r}")

    def to_market_row(self, profile_id: str, user_id: str, position: int) -> dict:
        return {
            "user_id": user_id,
            "profile_id": profile_id,
            "position": position,
            "market_id": self.market_id,
            "query_share": self.query_share,
            "locations": self.locations,
            "allowed_languages": self.allowed_languages,
            "currency": self.currency,
            "gross_base_floor": self.gross_base_floor,
            "location_floors": self.location_floors,
            "remote_policy": self.remote_policy,
            "relocation_policy": self.relocation_policy,
            "sponsorship_policy": self.sponsorship_policy,
            "direct_sources": self.direct_sources,
            "discovery_domains": self.discovery_domains,
            "query_templates": self.query_templates,
            "role_families": self.role_families,
            "enabled": self.enabled,
        }


class SearchProfile(BaseModel):
    # Assignment is validated as well as construction: callers edit a loaded
    # profile in place, so an out-of-range value must be refused where it is
    # set rather than surviving until the database check rejects the write.
    model_config = ConfigDict(validate_assignment=True)

    timezone: str = Field(min_length=1)
    scheduled_hour: int = Field(ge=0, le=23)
    max_jobs_per_run: int = Field(gt=0)
    source_minimum_per_run: int = Field(ge=0)
    source_max_share: float = Field(ge=0)
    thresholds: dict = Field(default_factory=dict)
    salary_floor_eur: int = Field(ge=0)

    daily_offer_limit: DailyOfferLimit = 10
    match_score_floor: int = Field(
        default=80, ge=_MATCH_SCORE_FLOOR_MIN, le=_MATCH_SCORE_FLOOR_MAX
    )

    target_titles: list[str] = Field(default_factory=list)
    positive_keywords: list[str] = Field(default_factory=list)
    blocked_title_keywords: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    search_query_templates: list[str] = Field(default_factory=list)
    search_domains: list[str] = Field(default_factory=list)
    specialist_search_domains: list[str] = Field(default_factory=list)
    specialist_query_templates: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    yc_job_pages: list[str] = Field(default_factory=list)

    engineering_title_keywords: list[str] = Field(default_factory=list)
    engineering_title_phrases: list[str] = Field(default_factory=list)
    blocked_profession_title_phrases: list[str] = Field(default_factory=list)
    specialist_board_hosts: list[str] = Field(default_factory=list)
    frontend_signals: list[str] = Field(default_factory=list)
    backend_heavy_signals: list[str] = Field(default_factory=list)

    max_search_queries_per_run: int = Field(gt=0)
    max_canonical_resolutions_per_run: int = Field(gt=0)
    max_learned_ats_boards_per_run: int = Field(gt=0)
    learned_ats_denylist: list[str] = Field(default_factory=list)
    learned_ats_allowlist: list[str] = Field(default_factory=list)

    manual_company_watch: list[dict] = Field(default_factory=list)
    ats: dict = Field(default_factory=dict)

    # What kind of employer this user wants (#198). Values come from
    # `company_facets`' controlled vocabularies -- the same ones extraction
    # writes -- because a preference naming something no company can ever be
    # is a silently dead setting, and the profile is where a person can still
    # be told about it. Empty means "no opinion", never "match nothing".
    preferred_industries: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)
    preferred_business_models: list[str] = Field(default_factory=list)
    excluded_business_models: list[str] = Field(default_factory=list)
    preferred_company_stages: list[str] = Field(default_factory=list)
    preferred_company_sizes: list[str] = Field(default_factory=list)

    markets: list[SearchProfileMarket] = Field(default_factory=list)

    def model_post_init(self, __context) -> None:
        for field_name, vocabulary in _COMPANY_PREFERENCE_VOCABULARIES.items():
            for value in getattr(self, field_name):
                if value not in vocabulary:
                    raise ValueError(
                        f"invalid {field_name} entry {value!r}: "
                        f"must be one of {sorted(vocabulary)}"
                    )

    def to_profile_row(self) -> dict:
        row = self.model_dump()
        row.pop("markets")
        return row

    def to_market_rows(self, profile_id: str, user_id: str = "") -> list[dict]:
        return [
            market.to_market_row(profile_id, user_id, index)
            for index, market in enumerate(self.markets)
        ]

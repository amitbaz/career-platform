from __future__ import annotations

from pydantic import BaseModel, Field

_REMOTE_POLICIES = {"preferred", "required", "allowed"}
_RELOCATION_POLICIES = {"none", "selective", "allowed"}
_SPONSORSHIP_POLICIES = {"not_required", "required"}


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

    def to_market_row(self, profile_id: str, user_id: str) -> dict:
        return {
            "user_id": user_id,
            "profile_id": profile_id,
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
    timezone: str = Field(min_length=1)
    scheduled_hour: int = Field(ge=0, le=23)
    max_jobs_per_run: int = Field(gt=0)
    source_minimum_per_run: int = Field(ge=0)
    source_max_share: float = Field(ge=0)
    thresholds: dict = Field(default_factory=dict)
    salary_floor_eur: int = Field(ge=0)

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

    markets: list[SearchProfileMarket] = Field(default_factory=list)

    def to_profile_row(self) -> dict:
        row = self.model_dump()
        row.pop("markets")
        return row

    def to_market_rows(self, profile_id: str, user_id: str = "") -> list[dict]:
        return [market.to_market_row(profile_id, user_id) for market in self.markets]

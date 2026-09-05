from job_hunter.models import MarketPolicy, SalaryPolicy, SearchPolicy


def make_market(
    market_id: str,
    share: float,
    *,
    locations: list[str] | None = None,
    currency: str = "EUR",
    floor: int = 90000,
    location_floors: dict[str, int] | None = None,
    languages: list[str] | None = None,
    remote_policy: str = "allowed",
    relocation_policy: str = "allowed",
    sponsorship_policy: str = "not_required",
    direct_sources: list[str] | None = None,
    discovery_domains: list[str] | None = None,
    templates: list[str] | None = None,
) -> MarketPolicy:
    return MarketPolicy(
        id=market_id,
        query_share=share,
        locations=locations or [market_id],
        allowed_languages=languages or ["English"],
        salary=SalaryPolicy(
            currency=currency,
            gross_base_floor=floor,
            location_floors=location_floors or {},
        ),
        remote_policy=remote_policy,
        relocation_policy=relocation_policy,
        sponsorship_policy=sponsorship_policy,
        direct_sources=direct_sources or [],
        discovery_domains=discovery_domains or ["wellfound.com", "jobs.ashbyhq.com"],
        query_templates=templates or ['"{role}" React TypeScript'],
    )


def make_market_policy(*, max_queries: int = 30) -> SearchPolicy:
    return SearchPolicy(
        target_titles=[
            "senior frontend engineer",
            "staff frontend engineer",
            "senior product engineer",
            "product engineer",
            "full-stack engineer",
        ],
        positive_keywords=["react", "typescript", "next.js", "node.js", "graphql"],
        blocked_title_keywords=["junior", "qa", "devops", "engineering manager"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        role_families=[
            "senior frontend engineer",
            "staff frontend engineer",
            "senior product engineer",
            "product engineer",
            "full-stack engineer",
        ],
        max_search_queries_per_run=max_queries,
        markets=[
            make_market("germany_eu", 0.35, locations=["Berlin", "Germany", "Europe"], remote_policy="preferred", relocation_policy="selective"),
            make_market("israel_remote", 0.25, locations=["Israel", "Tel Aviv"], currency="ILS", floor=420000, languages=["English", "Hebrew"], remote_policy="required", relocation_policy="none"),
            make_market("london", 0.17, locations=["London", "UK", "United Kingdom"], currency="GBP", floor=90000, sponsorship_policy="required"),
            make_market("singapore", 0.10, locations=["Singapore"], currency="SGD", floor=120000, sponsorship_policy="required"),
            make_market("us_nyc_sf", 0.10, locations=["New York", "NYC", "San Francisco", "Bay Area"], currency="USD", floor=180000, location_floors={"San Francisco": 200000, "Bay Area": 200000}, sponsorship_policy="required"),
            make_market("secondary_eu_relocation", 0.03, locations=["Amsterdam", "Paris", "Barcelona"], floor=70000, location_floors={"Amsterdam": 90000, "Paris": 80000, "Barcelona": 70000}, relocation_policy="selective"),
        ],
    )

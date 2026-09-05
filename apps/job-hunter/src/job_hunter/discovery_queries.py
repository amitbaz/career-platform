import hashlib
from datetime import date

from job_hunter.models import MarketPolicy, SearchPolicy, SearchQuery


def _rotation_key(run_date: date, market_id: str, text: str) -> str:
    return hashlib.sha256(
        f"{run_date.isoformat()}:{market_id}:{text}".encode("utf-8")
    ).hexdigest()


def allocate_market_query_slots(markets: list[MarketPolicy], budget: int) -> dict[str, int]:
    enabled = [m for m in markets if m.enabled]
    if not enabled or budget <= 0:
        return {}

    if budget < len(enabled):
        return {m.id: 1 for m in enabled[:budget]}

    allocation = {m.id: max(1, round(m.query_share * budget)) for m in enabled}

    def get_order_idx(market_id: str) -> int:
        for i, m in enumerate(enabled):
            if m.id == market_id:
                return i
        return 0

    while sum(allocation.values()) > budget:
        def over_alloc(m: MarketPolicy) -> tuple[float, int]:
            return (allocation[m.id] - (m.query_share * budget), -get_order_idx(m.id))
        target = max((m for m in enabled if allocation[m.id] > 1), key=over_alloc)
        allocation[target.id] -= 1

    while sum(allocation.values()) < budget:
        def under_alloc(m: MarketPolicy) -> tuple[float, int]:
            return ((m.query_share * budget) - allocation[m.id], -get_order_idx(m.id))
        target = max(enabled, key=under_alloc)
        allocation[target.id] += 1

    return allocation


def _generate_legacy_search_queries(policy: SearchPolicy) -> list[str]:
    legacy_queries: list[str] = []
    seen: set[str] = set()

    def add(query: str) -> None:
        normalized = " ".join(query.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            legacy_queries.append(normalized)

    for role in policy.role_families:
        for template in policy.search_query_templates:
            base = template.format(role=role)
            add(base)
            for domain in policy.search_domains:
                add(f"site:{domain} {base}")
    for query in policy.search_queries:
        add(query)

    if not policy.specialist_search_domains or not policy.specialist_query_templates:
        return legacy_queries[: policy.max_search_queries_per_run]

    specialist_slots = min(4, policy.max_search_queries_per_run)
    queries = legacy_queries[: policy.max_search_queries_per_run - specialist_slots]
    seen = set(queries)
    specialist_queries: list[str] = []

    def add_specialist(query: str) -> None:
        normalized = " ".join(query.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            specialist_queries.append(normalized)

    for role in policy.role_families:
        for template in policy.specialist_query_templates:
            base = template.format(role=role)
            for domain in policy.specialist_search_domains:
                add_specialist(f"site:{domain} {base}")

    return queries + specialist_queries[:specialist_slots]


def generate_search_queries(policy: SearchPolicy, run_date: date | None = None) -> list[SearchQuery]:
    if not policy.markets:
        legacy_str_queries = _generate_legacy_search_queries(policy)
        return [SearchQuery(text=q, market_id=None) for q in legacy_str_queries]

    if run_date is None:
        run_date = date.today()

    budget = policy.max_search_queries_per_run
    allocations = allocate_market_query_slots(policy.markets, budget)

    results: list[SearchQuery] = []
    seen: set[str] = set()

    for market in policy.markets:
        if market.id not in allocations:
            continue

        slots = allocations[market.id]
        roles = market.role_families or policy.role_families

        # Role families are rotated round-robin rather than ranked: the
        # market's first slot goes to its first role family, the second slot
        # to its second, and so on, so a market's slots spread over
        # min(len(roles), slots) distinct role families before any role is
        # searched twice. Treating role order as a hard priority tier instead
        # would let one role family's template/domain candidates consume every
        # slot a market has, leaving the rest of the list never searched.
        #
        # Within a role family all (template, source domain) combinations are
        # equal priority, so the date-seeded hash rotates them: each day a
        # different template/domain variant surfaces for the same role.
        market_candidates: list[tuple[tuple[int, int, str], str]] = []
        for r_idx, role in enumerate(roles):
            role_candidates: list[tuple[str, str]] = []
            local_seen: set[str] = set()
            for template in market.query_templates:
                base = " ".join(template.format(role=role).split())
                variants = [base] + [f"site:{domain} {base}" for domain in market.discovery_domains]
                for text in variants:
                    text = " ".join(text.split())
                    if not text or text in local_seen:
                        continue
                    local_seen.add(text)
                    role_candidates.append((_rotation_key(run_date, market.id, text), text))

            role_candidates.sort()
            for depth, (rotation_key, text) in enumerate(role_candidates):
                market_candidates.append(((depth, r_idx, rotation_key), text))

        market_candidates.sort(key=lambda candidate: candidate[0])

        selected = 0
        for _, text in market_candidates:
            if selected >= slots:
                break
            if text not in seen:
                seen.add(text)
                results.append(SearchQuery(text=text, market_id=market.id))
                selected += 1

    return results

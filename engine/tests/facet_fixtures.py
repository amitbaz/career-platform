"""One builder for the objective facets a posting carries.

Scoring is handed a posting's facets rather than its description (#126), so
both the evaluation module's tests and the pipeline's need to state what a
posting was read as. `tests/market_fixtures.py` is the precedent: shared
fixtures live beside the tests, not duplicated inside each file.
"""

from engine.models import Compensation, JobFacets

#: The single must-have a plain test posting states. Kept as a constant
#: because the number of stated requirements is a contract: a scoring
#: response must carry exactly one support verdict per requirement.
REACT_MUST_HAVE = {"requirement": "React", "depth": "experience", "kind": "must_have"}
GRAPHQL_PREFERRED = {"requirement": "GraphQL", "depth": "familiarity", "kind": "preferred"}


def make_facets(**overrides) -> JobFacets:
    """A well-formed set of facets, with any field overridden by keyword."""
    values = dict(
        seniority="senior",
        remote_policy="remote",
        relocation_policy="unknown",
        hiring_regions=["europe"],
        stack=["react", "typescript"],
        compensation=Compensation(),
        requirements=[REACT_MUST_HAVE, GRAPHQL_PREFERRED],
        source_supplied=[],
        model="gemini-test",
    )
    values.update(overrides)
    return JobFacets(**values)

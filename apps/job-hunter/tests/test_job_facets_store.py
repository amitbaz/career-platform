"""Persistence for objective facets (issue #125).

The two properties that matter here are that extraction happens once per
posting, and that a changed description invalidates the answer through the
*same* description-hash mechanism that already gates re-evaluation -- not a
second notion of a changed posting.
"""

import pytest

from job_hunter.models import Compensation, Job, JobFacets


def make_job(*, fingerprint: str = "facets", **overrides) -> Job:
    fields = {
        "source": "test",
        "title": "Engineer",
        "url": f"https://example.test/jobs/{fingerprint}",
        "description": "React and TypeScript.",
    }
    fields.update(overrides)
    return Job(**fields)


def _facets(**overrides) -> JobFacets:
    defaults = dict(
        seniority="senior",
        remote_policy="remote",
        relocation_policy="not_offered",
        hiring_regions=["europe"],
        stack=["react", "typescript"],
        compensation=Compensation(
            disclosed=True, currency="EUR", minimum=90000, maximum=120000, period="year"
        ),
        requirements=[
            {"requirement": "React", "depth": "experience", "kind": "must_have"}
        ],
        source_supplied=["remote_policy"],
        model="gemini-test",
    )
    defaults.update(overrides)
    return JobFacets(**defaults)


def test_facets_round_trip(store):
    job_id, _, _ = store.upsert_job(make_job())

    store.save_job_facets(job_id, _facets())
    stored = store.get_job_facets(job_id)

    assert stored is not None
    assert stored.seniority == "senior"
    assert stored.remote_policy == "remote"
    assert stored.relocation_policy == "not_offered"
    assert stored.hiring_regions == ["europe"]
    assert stored.stack == ["react", "typescript"]
    assert stored.compensation == Compensation(
        disclosed=True, currency="EUR", minimum=90000, maximum=120000, period="year"
    )
    assert stored.requirements == [
        {"requirement": "React", "depth": "experience", "kind": "must_have"}
    ]
    assert stored.source_supplied == ["remote_policy"]
    assert stored.model == "gemini-test"


def test_a_job_without_facets_has_none(store):
    job_id, _, _ = store.upsert_job(make_job())
    assert store.get_job_facets(job_id) is None


def test_saving_facets_twice_replaces_rather_than_appends(store):
    job_id, _, _ = store.upsert_job(make_job())

    store.save_job_facets(job_id, _facets())
    store.save_job_facets(job_id, _facets(seniority="staff"))

    assert store.get_job_facets(job_id).seniority == "staff"


def test_facets_are_stamped_with_the_jobs_current_description_hash(store):
    job = make_job(description="First description.")
    job_id, _, _ = store.upsert_job(job)

    store.save_job_facets(job_id, _facets())

    stored = store.get_job_facets(job_id)
    assert stored.description_hash_at_extraction
    # The caller never supplies the hash: the store reads it off the job, the
    # same way save_evaluation does, so there is only one notion of "the
    # description this was computed at".
    assert _facets().description_hash_at_extraction == ""


def test_a_job_with_no_facets_needs_extraction(store):
    job_id, _, _ = store.upsert_job(make_job())
    assert store.jobs_needing_facets([job_id]) == {job_id}


def test_a_job_with_current_facets_does_not_need_extraction(store):
    job_id, _, _ = store.upsert_job(make_job())
    store.save_job_facets(job_id, _facets())
    assert store.jobs_needing_facets([job_id]) == set()


def test_a_changed_description_invalidates_the_facets(store):
    job = make_job(description="First description.")
    job_id, _, _ = store.upsert_job(job)
    store.save_job_facets(job_id, _facets())
    assert store.jobs_needing_facets([job_id]) == set()

    changed = make_job(description="A materially different description.")
    same_id, _, description_changed = store.upsert_job(changed)

    assert same_id == job_id
    assert description_changed is True
    assert store.jobs_needing_facets([job_id]) == {job_id}


def test_re_extraction_after_a_change_clears_the_invalidation(store):
    job_id, _, _ = store.upsert_job(make_job(description="First."))
    store.save_job_facets(job_id, _facets())
    store.upsert_job(make_job(description="Second."))

    store.save_job_facets(job_id, _facets(seniority="staff"))

    assert store.jobs_needing_facets([job_id]) == set()
    assert store.get_job_facets(job_id).seniority == "staff"


def test_jobs_needing_facets_answers_for_many_jobs_at_once(store):
    fresh, _, _ = store.upsert_job(make_job(fingerprint="fresh"))
    enriched, _, _ = store.upsert_job(make_job(fingerprint="enriched"))
    store.save_job_facets(enriched, _facets())

    assert store.jobs_needing_facets([fresh, enriched, fresh]) == {fresh}


def test_jobs_needing_facets_ignores_a_job_that_cannot_be_read(store):
    # An id the caller cannot see has no description to extract from, so it
    # is not work this run can do -- claiming it needs extraction would put
    # the pipeline into a call it can only fail.
    missing = "00000000-0000-0000-0000-0000000000ff"
    assert store.jobs_needing_facets([missing]) == set()


def test_facets_of_a_job_merged_away_are_discarded_not_moved(store):
    # Unlike an evaluation, facets must not follow a merge. They describe the
    # posting they were read from; writing them against the survivor would
    # stamp them with the *survivor's* description hash, pinning one posting's
    # facts to another's text as permanently current with no path back to
    # re-extraction. The survivor is extracted from its own text later.
    survivor, _, _ = store.upsert_job(make_job(fingerprint="survivor"))
    duplicate, _, _ = store.upsert_job(make_job(fingerprint="duplicate"))
    store.merge_jobs(survivor, duplicate)

    store.save_job_facets(duplicate, _facets())

    assert store.get_job_facets(survivor) is None
    assert store.jobs_needing_facets([survivor]) == {survivor}


def test_undisclosed_compensation_round_trips_as_undisclosed(store):
    job_id, _, _ = store.upsert_job(make_job())
    store.save_job_facets(job_id, _facets(compensation=Compensation()))

    stored = store.get_job_facets(job_id).compensation
    assert stored.disclosed is False
    assert stored.minimum is None
    assert stored.maximum is None
    assert stored.currency == ""
    assert stored.period == ""

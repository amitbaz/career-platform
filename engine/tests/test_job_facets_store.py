"""Persistence for objective facets (issues #125, #175).

The properties that matter here are that extraction happens once per
posting -- across users, not merely across runs (#175) -- and that a changed
description invalidates the answer through the *same* description-hash
mechanism that already gates re-evaluation, not a second notion of a changed
posting.

The store still speaks in job ids, because that is what the pipeline holds.
Everything it stores is keyed on the posting the job is a copy of, which is
why a second user reads what the first user's run paid for.
"""

import uuid
from datetime import datetime, timezone

import pytest

from engine.models import Compensation, Job, JobFacets
from engine.store_mapping import to_iso
from engine.supabase_client import SupabaseRequestError


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


def test_facets_are_stamped_with_the_postings_current_description_hash(store):
    job = make_job(description="First description.")
    job_id, _, _ = store.upsert_job(job)

    store.save_job_facets(job_id, _facets())

    stored = store.get_job_facets(job_id)
    assert stored.description_hash_at_extraction
    # The caller never supplies the hash: the store reads it off the posting,
    # the same way save_evaluation reads it off the job, so there is only one
    # notion of "the description this was computed at" -- and since #175 it
    # is the shared one, which is what makes a re-read cost one call rather
    # than one per user.
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
    # stamp them with the *survivor* posting's description hash, pinning one
    # posting's facts to another's text as permanently current with no path
    # back to re-extraction. The survivor is extracted from its own text
    # later. A merged-away job row is gone, so it resolves to no posting at
    # all -- which is how the store recognises this case.
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


# --- One extraction for everyone (issue #175) --------------------------------
#
# The prompt cannot see who is asking (#126), so the answer is a property of
# the advertisement. These are the acceptance criteria of #175: what one
# user's run reads, every user's run has.


def test_a_second_users_run_reads_the_first_users_facets(store, other_store):
    job = make_job(fingerprint="shared-advertisement")
    mine, _, _ = store.upsert_job(job)
    theirs, _, _ = other_store.upsert_job(job)
    # Two job rows -- each user keeps their own copy of the relationship --
    # over one posting.
    assert mine != theirs

    store.save_job_facets(mine, _facets(seniority="staff"))

    # Nothing for the second user to extract, and the answer is already there.
    assert other_store.jobs_needing_facets([theirs]) == set()
    assert other_store.get_job_facets(theirs).seniority == "staff"


def test_a_changed_description_is_re_extracted_once_for_everyone(store, other_store):
    job = make_job(fingerprint="edited-advertisement", description="First description.")
    mine, _, _ = store.upsert_job(job)
    theirs, _, _ = other_store.upsert_job(job)
    store.save_job_facets(mine, _facets())
    assert store.jobs_needing_facets([mine]) == set()
    assert other_store.jobs_needing_facets([theirs]) == set()

    # The advertisement is edited, and one user sees it first.
    edited = make_job(
        fingerprint="edited-advertisement",
        description="A materially different description of the same job.",
    )
    store.upsert_job(edited)

    # Both users' runs now agree it needs re-reading, because both are asking
    # about the same posting...
    assert store.jobs_needing_facets([mine]) == {mine}
    assert other_store.jobs_needing_facets([theirs]) == {theirs}

    # ...and one read settles it for both. Two users can no longer invalidate
    # each other's extraction in turn.
    other_store.save_job_facets(theirs, _facets(seniority="lead"))

    assert store.jobs_needing_facets([mine]) == set()
    assert other_store.jobs_needing_facets([theirs]) == set()
    assert store.get_job_facets(mine).seniority == "lead"


def test_one_users_re_extraction_replaces_the_other_users_answer(store, other_store):
    # One posting, one set of facets: a second write must replace the first
    # rather than leave the two users looking at different answers.
    job = make_job(fingerprint="one-answer")
    mine, _, _ = store.upsert_job(job)
    theirs, _, _ = other_store.upsert_job(job)

    store.save_job_facets(mine, _facets(seniority="senior"))
    other_store.save_job_facets(theirs, _facets(seniority="principal"))

    assert store.get_job_facets(mine).seniority == "principal"
    assert other_store.get_job_facets(theirs).seniority == "principal"


def test_a_job_with_no_posting_cannot_be_written(store, supabase_client):
    # A job row written without going through job_hunter_upsert_job used to
    # carry no posting_id -- the SQLite migration script did exactly this --
    # and the store answered "no facets, no work to do" for it, so a run could
    # not spend a provider call whose result had nowhere to go.
    #
    # #178 removed the case: posting_id is `not null`. The store's guards are
    # unchanged and still cover a job id it cannot read; what is asserted here
    # is that a row without an advertisement is refused at the door.
    now = to_iso(datetime.now(timezone.utc))
    with pytest.raises(SupabaseRequestError):
        supabase_client.insert(
            "job_hunter_jobs",
            [
                {
                    "user_id": supabase_client.user_id,
                    "first_seen_at": now,
                    "last_seen_at": now,
                }
            ],
        )

    # A job id the caller cannot read is still answered, not raised: the run
    # keeps going.
    unknown = str(uuid.uuid4())
    assert store.jobs_needing_facets([unknown]) == set()
    assert store.get_job_facets(unknown) is None
    store.save_job_facets(unknown, _facets())
    assert store.get_job_facets(unknown) is None

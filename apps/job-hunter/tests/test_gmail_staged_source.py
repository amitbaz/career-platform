from job_hunter.gmail_models import ExtractedJob
from job_hunter.job_identity import job_fallback_identity
from job_hunter.models import Job
from job_hunter.sources import GmailStagedSource


class FakeStagedJobStore:
    def __init__(self, rows):
        self._rows = rows

    def list_eligible_inbound_jobs(self):
        return self._rows


def test_staged_source_returns_stable_gmail_job_identity(store, tmp_path):
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            source_job_id="job-123",
            url="https://linkedin.example/jobs/123",
            company="Acme",
            title="Senior Product Engineer",
            location="Remote",
            remote=True,
            description="React TypeScript",
        ),
    )

    jobs = GmailStagedSource(store).discover()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "gmail:linkedin"
    assert job.source_job_id == "linkedin:job-123"
    assert job.title == "Senior Product Engineer"
    assert job.company == "Acme"
    assert job.location == "Remote"
    assert job.url == "https://linkedin.example/jobs/123"
    assert job.description == ""
    assert job.remote is True


def test_staged_source_normalizes_linkedin_hiring_page_title():
    store = FakeStagedJobStore(
        [
            {
                "source_platform": "linkedin",
                "source_candidate_key": "linkedin:job-123",
                "url": "https://www.linkedin.com/jobs/view/123",
                "title": "Magentic hiring Senior Frontend Engineer in London, England, United Kingdom | LinkedIn",
                "company": "",
                "location": "",
                "description": "",
                "remote": None,
            }
        ]
    )

    [job] = GmailStagedSource(store).discover()

    assert job.title == "Senior Frontend Engineer"
    assert job.company == "Magentic"
    assert job.location == "London, England, United Kingdom"
    assert job_fallback_identity(job.company, job.title, job.location) == (
        "magentic|senior frontend engineer|london england united kingdom"
    )


def test_staged_source_keeps_existing_linkedin_metadata_over_page_title():
    store = FakeStagedJobStore(
        [
            {
                "source_platform": "linkedin",
                "source_candidate_key": "linkedin:job-123",
                "url": "https://www.linkedin.com/jobs/view/123",
                "title": "Magentic hiring Senior Frontend Engineer in London, England, United Kingdom | LinkedIn",
                "company": "Magentic GmbH",
                "location": "Remote in the UK",
                "description": "",
                "remote": None,
            }
        ]
    )

    [job] = GmailStagedSource(store).discover()

    assert job.title == "Senior Frontend Engineer"
    assert job.company == "Magentic GmbH"
    assert job.location == "Remote in the UK"


def test_staged_source_does_not_guess_from_an_unrecognized_linkedin_title():
    store = FakeStagedJobStore(
        [
            {
                "source_platform": "linkedin",
                "source_candidate_key": "linkedin:job-123",
                "url": "https://www.linkedin.com/jobs/view/123",
                "title": "Magentic jobs | LinkedIn",
                "company": "",
                "location": "",
                "description": "",
                "remote": None,
            }
        ]
    )

    [job] = GmailStagedSource(store).discover()

    assert job.title == "Magentic jobs | LinkedIn"
    assert job.company == ""
    assert job.location == ""


def test_same_canonical_url_on_unevaluated_public_job_is_still_emitted(store):
    """A public job that has not been evaluated must not suppress the candidate.

    This is the retry #96 exists for: a Gmail posting that was materialized
    but missed a shortlist has to come back on the next run.
    """
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            url="https://jobs.example.com/role?utm_source=linkedin",
            company="Email Company",
            title="Email Title",
        ),
    )
    store.upsert_job(
        Job(
            source="public",
            source_job_id="public-123",
            url="https://jobs.example.com/role",
            company="Public Company",
            title="Public Title",
        )
    )

    [job] = GmailStagedSource(store).discover()

    assert job.source == "gmail:linkedin"
    assert job.source_job_id == "linkedin:job-123"


def test_same_canonical_url_on_closed_public_job_is_not_emitted(store):
    """A terminal public job suppresses the candidate across sources."""
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            url="https://jobs.example.com/role?utm_source=linkedin",
            company="Email Company",
            title="Email Title",
        ),
    )
    job_id, _, _ = store.upsert_job(
        Job(
            source="public",
            source_job_id="public-123",
            url="https://jobs.example.com/role",
            company="Public Company",
            title="Public Title",
        )
    )
    store.set_job_status(job_id, "closed")

    assert GmailStagedSource(store).discover() == []


def test_same_identity_on_unevaluated_public_job_is_still_emitted(store):
    """Identity matching alone does not suppress; completeness does."""
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            company="  ACME  ",
            title="Senior   Frontend Engineer",
            location="Berlin",
        ),
    )
    store.upsert_job(
        Job(
            source="public",
            source_job_id="public-123",
            url="https://jobs.example.com/role",
            company="Acme",
            title="senior frontend engineer",
            location=" berlin ",
        )
    )

    [job] = GmailStagedSource(store).discover()

    assert job.source == "gmail:linkedin"
    assert job.source_job_id == "linkedin:job-123"


def test_same_identity_on_closed_public_job_is_not_emitted(store):
    """The normalized company/title/location branch honours terminal status.

    Proves the identity match really is what suppresses here: the two jobs
    share no URL at all, only normalized company, title and location.
    """
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            company="  ACME  ",
            title="Senior   Frontend Engineer",
            location="Berlin",
        ),
    )
    job_id, _, _ = store.upsert_job(
        Job(
            source="public",
            source_job_id="public-123",
            url="https://jobs.example.com/role",
            company="Acme",
            title="senior frontend engineer",
            location=" berlin ",
        )
    )
    store.set_job_status(job_id, "closed")

    assert GmailStagedSource(store).discover() == []

from job_hunter.gmail_models import ExtractedJob
from job_hunter.models import Job
from job_hunter.sources import GmailStagedSource
from job_hunter.store import JobStore


def test_staged_source_returns_stable_gmail_job_identity(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
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


def test_same_canonical_url_already_materialized_by_public_source_is_not_emitted(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
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

    assert GmailStagedSource(store).discover() == []


def test_same_identity_already_materialized_by_public_source_is_not_emitted(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
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

    assert GmailStagedSource(store).discover() == []

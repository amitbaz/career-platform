from job_hunter.models import Job
from job_hunter.normalize import canonicalize_url, job_fingerprint


def test_canonicalize_url_drops_tracking_and_fragment():
    url = "https://example.com/jobs/42?utm_source=x&gh_src=abc&keep=1#apply"
    assert canonicalize_url(url) == "https://example.com/jobs/42?keep=1"


def test_canonicalize_url_drops_utm_variants():
    url = "https://example.com/jobs/1?utm_medium=email&utm_campaign=foo"
    assert canonicalize_url(url) == "https://example.com/jobs/1"


def test_canonicalize_url_drops_lever_source():
    url = "https://jobs.lever.co/acme/123?lever-source=linkedin"
    assert canonicalize_url(url) == "https://jobs.lever.co/acme/123"


def test_canonicalize_url_keeps_functional_params():
    url = "https://example.com/jobs?page=2&category=engineering"
    assert canonicalize_url(url) == "https://example.com/jobs?category=engineering&page=2"


def test_fingerprint_prefers_source_job_id():
    job = Job(source="ashby", source_job_id="abc", url="https://x/y", company="X", title="Senior Product Engineer")
    assert job_fingerprint(job) == job_fingerprint(
        Job(source="ashby", source_job_id="abc", url="https://different", company="Y", title="Other")
    )


def test_fingerprint_falls_back_to_url():
    job1 = Job(source="web", url="https://example.com/jobs/42", title="Senior Product Engineer", company="X")
    job2 = Job(source="web", url="https://example.com/jobs/42?utm_source=x", title="Other", company="Y")
    assert job_fingerprint(job1) == job_fingerprint(job2)


def test_fingerprint_unique_for_different_ids():
    job1 = Job(source="ashby", source_job_id="abc", title="X", company="X")
    job2 = Job(source="ashby", source_job_id="xyz", title="X", company="X")
    assert job_fingerprint(job1) != job_fingerprint(job2)

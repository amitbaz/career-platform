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


def test_fingerprint_prefers_ats_triple_across_source_labels():
    # Every known ATS adapter reads source_job_id and the ATS job id off the
    # same field of the same listing, so they agree -- ashby.py, greenhouse.py
    # and lever.py all do this, and a company-watch relabel never touches
    # either field. Matches the live shape: watch:ashby carries the same
    # source_job_id the direct ashby crawl does.
    direct = Job(
        source="ashby",
        source_job_id="999",
        ats_provider="ashby",
        ats_board="bjak",
        ats_job_id="999",
        title="X",
        company="X",
    )
    via_watch = Job(
        source="watch:ashby",
        source_job_id="999",
        ats_provider="ashby",
        ats_board="bjak",
        ats_job_id="999",
        title="X",
        company="X",
    )
    assert job_fingerprint(direct) == job_fingerprint(via_watch)


def test_fingerprint_distrusts_ats_identity_disagreeing_with_source_job_id():
    # #254's shape: a listing whose source_job_id and url are self-consistent
    # but whose ats_job_id has been cross-contaminated with a different job's
    # id. Trusting it here would hash two different advertisements
    # identically -- the fingerprint must fall back to the source-scoped key
    # instead of folding them together.
    corrupted = Job(
        source="greenhouse",
        source_job_id="8612482002",
        ats_provider="greenhouse",
        ats_board="alarmcom",
        ats_job_id="8648918002",  # belongs to a different real job ad
        title="X",
        company="X",
    )
    assert job_fingerprint(corrupted) == job_fingerprint(
        Job(source="greenhouse", source_job_id="8612482002", title="Y", company="Y")
    )


def test_fingerprint_trusts_ats_identity_when_source_job_id_absent():
    # An aggregator sighting that never learned a source_job_id has nothing
    # to disagree with the ats identity, so the triple is still trusted.
    job = Job(
        source="weworkremotely",
        ats_provider="ashby",
        ats_board="stickermule",
        ats_job_id="abc",
        title="X",
        company="X",
    )
    assert job_fingerprint(job) == job_fingerprint(
        Job(source="ashby", source_job_id="abc", ats_provider="ashby",
            ats_board="stickermule", ats_job_id="abc", title="Y", company="Y")
    )


def test_fingerprint_falls_back_when_ats_triple_incomplete():
    job1 = Job(source="ashby", source_job_id="abc", ats_provider="ashby", title="X", company="X")
    job2 = Job(source="watch:ashby", source_job_id="abc", ats_provider="ashby", title="X", company="X")
    assert job_fingerprint(job1) != job_fingerprint(job2)


def test_fingerprint_ats_triple_distinguishes_different_boards():
    job1 = Job(source="ashby", ats_provider="ashby", ats_board="bjak", ats_job_id="999", title="X", company="X")
    job2 = Job(source="watch:ashby", ats_provider="ashby", ats_board="other", ats_job_id="999", title="X", company="X")
    assert job_fingerprint(job1) != job_fingerprint(job2)

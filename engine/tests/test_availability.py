from engine.availability import CLOSED, UNCHECKED, VERIFIED, detect_closure


def test_detects_expired_posting_banner():
    html = "<html><body><h1>This job posting has expired</h1></body></html>"
    assert detect_closure(html) is True


def test_detects_no_longer_accepting_applications():
    html = "<div>We are no longer accepting applications for this role.</div>"
    assert detect_closure(html) is True


def test_detects_position_filled():
    html = "<p>This position has been filled.</p>"
    assert detect_closure(html) is True


def test_normal_active_posting_is_not_closed():
    html = "<html><body><h1>Senior Frontend Engineer</h1><p>Apply now.</p></body></html>"
    assert detect_closure(html) is False


def test_unrelated_no_longer_available_phrase_is_not_a_false_positive():
    html = "<p>This discount is no longer available in your region.</p>"
    assert detect_closure(html) is False


def test_recruiter_agency_disclaimer_is_not_a_false_positive():
    html = (
        "<p>We are not accepting applications from recruiters or agencies "
        "for this role.</p>"
    )
    assert detect_closure(html) is False


def test_cohort_scoped_closure_disclaimer_is_not_a_false_positive():
    html = "<p>Applications for this cohort are closed; the next cohort opens in March.</p>"
    assert detect_closure(html) is False


def test_constants_are_distinct():
    assert len({CLOSED, VERIFIED, UNCHECKED}) == 3

"""HTTP-layer tests for the Engine Lab review page (#257).

`test_engine_lab.py` covers `engine_lab.py` (login/session handling, card
selection, judgements, summary) against fakes; this file covers routing,
session handling and HTML rendering in `engine_lab_web.py` -- especially
cohort concealment, which the migration comment says plainly is enforced
only by the application: "concealment is enforced by the application, not
by this table." Nothing in the schema or in `engine_lab.py` would catch a
future template edit that leaked `card.cohort` into the review page, so
this is the one place that has to.

Every collaborator, GoTrue and Supabase call is faked at the `engine_lab`
module boundary -- these tests exercise the Flask app only, never real
network or database calls.
"""

from __future__ import annotations

import dataclasses
import time

import pytest
from flask import Flask

from job_hunter import engine_lab, engine_lab_web
from job_hunter.config import SupabaseSettings

_USER_ID = "00000000-0000-0000-0000-0000000000a1"
_EMAIL = "reviewer@test.local"


def _fake_settings() -> SupabaseSettings:
    return SupabaseSettings(
        user_id="00000000-0000-0000-0000-000000000001",
        url="http://example.invalid",
        publishable_key="pub",
        signing_key_jwk={"d": "x", "kid": "k"},
    )


def _fake_auth_session(expires_in: float = 3600) -> engine_lab.AuthSession:
    return engine_lab.AuthSession(
        user_id=_USER_ID,
        email=_EMAIL,
        access_token="access-token",
        refresh_token="refresh-token",
        expires_at=time.time() + expires_in,
    )


def _fake_collaborator(is_owner: bool = False) -> engine_lab.Collaborator:
    return engine_lab.Collaborator(user_id=_USER_ID, email=_EMAIL, is_owner=is_owner, revoked_at=None)


def _fake_card(impression_id: str = "impression-1") -> engine_lab.ReviewCard:
    return engine_lab.ReviewCard(
        impression_id=impression_id,
        posting_title="Engineer",
        posting_company="Acme",
        posting_location="Remote",
        posting_url="https://example.invalid/job/1",
        why_line="Strong fit.",
        profile_version="p1",
        posting_version="pv1",
        matching_version="m1",
        explanation_version="e1",
        configuration_version="c1",
    )


@pytest.fixture
def app(monkeypatch) -> Flask:
    monkeypatch.setenv("ENGINE_LAB_SESSION_SECRET", "test-secret")
    monkeypatch.setattr(engine_lab_web, "load_supabase_settings", _fake_settings)
    monkeypatch.setattr(engine_lab, "session_client", lambda http, settings, auth_session: object())
    monkeypatch.setattr(engine_lab, "subject_store_client", lambda http, settings: object())
    monkeypatch.setattr(engine_lab, "shown_posting_ids_today", lambda client, reviewer_id: set())

    flask_app = Flask(__name__)
    engine_lab_web.register_engine_lab_routes(flask_app, object())
    return flask_app


def _log_in(client, *, is_owner: bool = False, monkeypatch=None) -> None:
    with client.session_transaction() as flask_session:
        auth_session = _fake_auth_session()
        flask_session[engine_lab_web._SESSION_KEY] = {
            "user_id": auth_session.user_id,
            "email": auth_session.email,
            "access_token": auth_session.access_token,
            "refresh_token": auth_session.refresh_token,
            "expires_at": auth_session.expires_at,
        }
    if monkeypatch is not None:
        monkeypatch.setattr(
            engine_lab, "get_own_collaborator", lambda client, user_id: _fake_collaborator(is_owner=is_owner)
        )


def test_unauthenticated_request_is_redirected_to_login(app):
    response = app.test_client().get("/engine-lab/review")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/engine-lab/login")


def test_login_sends_a_code_and_moves_to_the_code_page(app, monkeypatch):
    sent = {}

    def fake_send_login_code(http, settings, email):
        sent["email"] = email

    monkeypatch.setattr(engine_lab, "send_login_code", fake_send_login_code)
    client = app.test_client()

    response = client.post("/engine-lab/login", data={"email": "new@test.local"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/engine-lab/login/code")
    assert sent["email"] == "new@test.local"


def test_wrong_code_is_refused_without_creating_a_session(app, monkeypatch):
    monkeypatch.setattr(
        engine_lab,
        "verify_login_code",
        lambda http, settings, email, code: (_ for _ in ()).throw(engine_lab.LoginError("bad code")),
    )
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session[engine_lab_web._PENDING_EMAIL_KEY] = _EMAIL

    response = client.post("/engine-lab/login/code", data={"code": "000000"})

    assert response.status_code == 401
    with client.session_transaction() as flask_session:
        assert engine_lab_web._SESSION_KEY not in flask_session


def test_correct_code_for_an_uninvited_email_is_refused(app, monkeypatch):
    monkeypatch.setattr(engine_lab, "verify_login_code", lambda *a, **k: _fake_auth_session())
    monkeypatch.setattr(engine_lab, "bootstrap_owner_if_matching", lambda *a, **k: None)
    monkeypatch.setattr(engine_lab, "claim_invite", lambda client: False)
    monkeypatch.setattr(engine_lab, "get_own_collaborator", lambda client, user_id: None)
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session[engine_lab_web._PENDING_EMAIL_KEY] = _EMAIL

    response = client.post("/engine-lab/login/code", data={"code": "123456"})

    assert response.status_code == 403
    with client.session_transaction() as flask_session:
        assert engine_lab_web._SESSION_KEY not in flask_session


def test_correct_code_for_an_invited_email_creates_a_session(app, monkeypatch):
    monkeypatch.setattr(engine_lab, "verify_login_code", lambda *a, **k: _fake_auth_session())
    monkeypatch.setattr(engine_lab, "bootstrap_owner_if_matching", lambda *a, **k: None)
    monkeypatch.setattr(engine_lab, "claim_invite", lambda client: True)
    monkeypatch.setattr(engine_lab, "get_own_collaborator", lambda client, user_id: _fake_collaborator())
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session[engine_lab_web._PENDING_EMAIL_KEY] = _EMAIL

    response = client.post("/engine-lab/login/code", data={"code": "123456"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/engine-lab/review")
    with client.session_transaction() as flask_session:
        assert flask_session[engine_lab_web._SESSION_KEY]["user_id"] == _USER_ID


def test_review_page_never_mentions_any_cohort_name(app, monkeypatch):
    _log_in(app.test_client(), monkeypatch=monkeypatch)  # warm the monkeypatch target
    monkeypatch.setattr(engine_lab, "select_next_card", lambda *args, **kwargs: _fake_card())
    client = app.test_client()
    _log_in(client, monkeypatch=monkeypatch)

    response = client.get("/engine-lab/review")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for cohort in engine_lab.COHORTS:
        assert cohort not in body, f"{cohort!r} leaked into the review page before judging"
    assert "Engineer" in body
    assert "Strong fit." in body


def test_review_page_never_links_a_javascript_scheme_posting_url(app, monkeypatch):
    unsafe_card = dataclasses.replace(_fake_card(), posting_url="javascript:alert(1)")
    monkeypatch.setattr(engine_lab, "select_next_card", lambda *args, **kwargs: unsafe_card)
    client = app.test_client()
    _log_in(client, monkeypatch=monkeypatch)

    body = client.get("/engine-lab/review").get_data(as_text=True)

    assert "javascript:" not in body


def test_judge_reveals_the_cohort_only_after_recording_the_judgement(app, monkeypatch):
    recorded: dict = {}

    def fake_record_judgement(client, **kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(engine_lab, "record_judgement", fake_record_judgement)
    monkeypatch.setattr(engine_lab, "reveal_cohort", lambda client, impression_id: "audit_hard_excluded")

    client = app.test_client()
    _log_in(client, monkeypatch=monkeypatch)

    response = client.post(
        "/engine-lab/judge",
        data={"impression_id": "impression-1", "worth_applying": "true", "why_line_judgement": "helpful"},
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "audit_hard_excluded" in body
    assert recorded == {
        "reviewer_id": _USER_ID,
        "impression_id": "impression-1",
        "worth_applying": True,
        "why_line_judgement": "helpful",
        "problem_reason": None,
    }


def test_judge_rejects_a_request_missing_the_worth_applying_field(app, monkeypatch):
    called = False

    def fake_record_judgement(client, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(engine_lab, "record_judgement", fake_record_judgement)
    client = app.test_client()
    _log_in(client, monkeypatch=monkeypatch)

    response = client.post(
        "/engine-lab/judge",
        data={"impression_id": "impression-1", "why_line_judgement": "helpful"},
    )

    assert response.status_code == 400
    assert not called, "an invalid judgement must never reach the ledger"


def test_invite_page_is_refused_to_a_non_owner(app, monkeypatch):
    client = app.test_client()
    _log_in(client, is_owner=False, monkeypatch=monkeypatch)

    response = client.get("/engine-lab/invite")

    assert response.status_code == 403


def test_invite_page_lets_the_owner_invite_a_collaborator(app, monkeypatch):
    invited = {}

    def fake_invite(client, email):
        invited["email"] = email

    monkeypatch.setattr(engine_lab, "invite_collaborator", fake_invite)
    monkeypatch.setattr(engine_lab, "list_collaborators", lambda client: [])
    client = app.test_client()
    _log_in(client, is_owner=True, monkeypatch=monkeypatch)

    response = client.post("/engine-lab/invite", data={"email": "new-collaborator@test.local"})

    assert response.status_code == 302
    assert invited["email"] == "new-collaborator@test.local"


def test_summary_page_is_refused_to_a_non_owner(app, monkeypatch):
    client = app.test_client()
    _log_in(client, is_owner=False, monkeypatch=monkeypatch)

    response = client.get("/engine-lab/summary")

    assert response.status_code == 403


def test_summary_page_names_a_cohort_with_zero_impressions(app, monkeypatch):
    def fake_daily_summary(client, day):
        return [
            engine_lab.CohortSummary(cohort, 0 if cohort != "intended" else 1, 0, None, None)
            for cohort in engine_lab.COHORTS
        ]

    monkeypatch.setattr(engine_lab, "daily_summary", fake_daily_summary)
    client = app.test_client()
    _log_in(client, is_owner=True, monkeypatch=monkeypatch)

    body = client.get("/engine-lab/summary").get_data(as_text=True)

    assert "Missing cohort" in body
    assert "audit_hard_excluded" in body

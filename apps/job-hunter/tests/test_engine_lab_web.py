"""HTTP-layer tests for the Engine Lab review page (#257).

`test_engine_lab.py` covers `engine_lab.py` (card selection, judgements,
summary) against fakes; this file covers the one property that lives only
in `engine_lab_web.py`'s rendering -- cohort concealment. The migration
comment says it plainly: "concealment is enforced by the application, not
by this table." Nothing in the schema or in `engine_lab.py` would catch a
future template edit that leaked `card.cohort` into the review page, so
this is the one place that has to.

Every collaborator, store and Supabase call is faked at the `engine_lab`
module boundary -- these tests exercise routing, session handling and HTML
rendering only, never real network or database calls.
"""

from __future__ import annotations

import dataclasses

import pytest
from flask import Flask

from job_hunter import engine_lab, engine_lab_web
from job_hunter.config import SupabaseSettings

_REVIEWER_ID = "reviewer-1"


def _fake_settings() -> SupabaseSettings:
    return SupabaseSettings(
        user_id="00000000-0000-0000-0000-000000000001",
        url="http://example.invalid",
        publishable_key="pub",
        signing_key_jwk={"d": "x", "kid": "k"},
    )


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
    monkeypatch.setattr(engine_lab, "runner_client", lambda http, settings: object())
    monkeypatch.setattr(engine_lab, "reviewer_client", lambda http, settings, reviewer_id: object())
    monkeypatch.setattr(
        engine_lab,
        "get_active_collaborator",
        lambda client, user_id: engine_lab.Collaborator(
            user_id=user_id, email="reviewer@test.local", display_name="", revoked_at=None
        ),
    )
    monkeypatch.setattr(engine_lab, "shown_posting_ids_today", lambda client, reviewer_id: set())

    flask_app = Flask(__name__)
    engine_lab_web.register_engine_lab_routes(flask_app, object())
    return flask_app


def _log_in(client) -> None:
    with client.session_transaction() as flask_session:
        flask_session[engine_lab_web._SESSION_KEY] = _REVIEWER_ID


def test_unauthenticated_request_is_redirected_to_login(app):
    response = app.test_client().get("/engine-lab/review")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/engine-lab/login")


def test_review_page_never_mentions_any_cohort_name(app, monkeypatch):
    monkeypatch.setattr(engine_lab, "select_next_card", lambda *args, **kwargs: _fake_card())
    client = app.test_client()
    _log_in(client)

    response = client.get("/engine-lab/review")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for cohort in engine_lab.COHORTS:
        assert cohort not in body, f"{cohort!r} leaked into the review page before judging"
    # The card content itself does render -- this isn't just an empty page.
    assert "Engineer" in body
    assert "Strong fit." in body


def test_review_page_never_links_a_javascript_scheme_posting_url(app, monkeypatch):
    unsafe_card = dataclasses.replace(_fake_card(), posting_url="javascript:alert(1)")
    monkeypatch.setattr(engine_lab, "select_next_card", lambda *args, **kwargs: unsafe_card)
    client = app.test_client()
    _log_in(client)

    body = client.get("/engine-lab/review").get_data(as_text=True)

    assert "javascript:" not in body


def test_judge_reveals_the_cohort_only_after_recording_the_judgement(app, monkeypatch):
    recorded: dict = {}

    def fake_record_judgement(client, **kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(engine_lab, "record_judgement", fake_record_judgement)
    monkeypatch.setattr(engine_lab, "reveal_cohort", lambda client, impression_id: "audit_hard_excluded")

    client = app.test_client()
    _log_in(client)

    response = client.post(
        "/engine-lab/judge",
        data={
            "impression_id": "impression-1",
            "worth_applying": "true",
            "why_line_judgement": "helpful",
        },
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "audit_hard_excluded" in body
    assert recorded == {
        "reviewer_id": _REVIEWER_ID,
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
    _log_in(client)

    response = client.post(
        "/engine-lab/judge",
        data={"impression_id": "impression-1", "why_line_judgement": "helpful"},
    )

    assert response.status_code == 400
    assert not called, "an invalid judgement must never reach the ledger"

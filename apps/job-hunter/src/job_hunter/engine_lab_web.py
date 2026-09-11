"""HTTP routes for the private Engine Lab review page (#257).

Registered onto the same Flask app the Telegram webhook already uses
(`telegram_webhook.create_app`) -- see the design doc's "Where this lives"
section for why this is one Vercel function rather than a page in
`apps/relay`.

This module renders HTML and turns a request into a call into `engine_lab.py`.
It contains no identity, selection, ranking or explanation logic of its own --
every decision about who is signed in, which posting is shown, what its why
line says, or which cohort it belongs to is made by that module, never here.

All posting/evaluation text rendered on these pages came from a scraped job
advertisement or a model's own output -- untrusted content -- so every such
value is passed through `markupsafe.escape` before it reaches an HTML string.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone

from flask import Flask, redirect, request, session, url_for
from markupsafe import escape

from job_hunter import engine_lab
from job_hunter.config import load_supabase_settings
from job_hunter.http import HttpClient
from job_hunter.postgres_store import PostgresJobStore

logger = logging.getLogger(__name__)

_SESSION_KEY = "engine_lab_session"
_PENDING_EMAIL_KEY = "engine_lab_pending_email"


def _safe_link(url: str) -> str:
    """A posting's own URL, rendered as a link only if it is actually a link.

    `markupsafe.escape` stops HTML injection but not a `javascript:`-scheme
    value -- and a posting's URL is scraped, untrusted text like every other
    field on the card. Restricting to http(s) is what makes `<a href=...>`
    safe to build from it at all.
    """
    if url.startswith(("http://", "https://")):
        return url
    return ""


def _page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title></head>"
        f"<body style=\"font-family: sans-serif; max-width: 640px; margin: 2rem auto;\">"
        f"{body}</body></html>"
    )


def register_engine_lab_routes(app: Flask, http: HttpClient) -> None:
    """Add `/engine-lab/*` routes to `app`.

    Every route below resolves settings and clients lazily, on the request
    that needs them -- matching `create_app`'s own lazy-configuration
    pattern, so `/health` and every unrelated route keep working even when
    `ENGINE_LAB_SESSION_SECRET`/`ENGINE_LAB_OWNER_EMAIL` are unset. A
    request that actually reaches a session-backed route with no secret key
    fails there, loudly, which is the correct behaviour for a
    misconfigured deployment rather than a silent bypass of the login check.
    """
    if not app.secret_key:
        app.secret_key = os.environ.get("ENGINE_LAB_SESSION_SECRET")
    # Defense in depth alongside modern browsers' own default (unset
    # SameSite is already treated as Lax): a reviewer's session cookie
    # must never ride along on a cross-site request, and this deployment
    # is HTTPS-only, so there is no reason not to require it.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = True

    def _owner_email() -> str:
        return os.environ.get("ENGINE_LAB_OWNER_EMAIL", "")

    def _store_session(auth_session: engine_lab.AuthSession) -> None:
        session[_SESSION_KEY] = {
            "user_id": auth_session.user_id,
            "email": auth_session.email,
            "access_token": auth_session.access_token,
            "refresh_token": auth_session.refresh_token,
            "expires_at": auth_session.expires_at,
        }

    def _current_auth_session() -> engine_lab.AuthSession | None:
        raw = session.get(_SESSION_KEY)
        if not raw:
            return None
        auth_session = engine_lab.AuthSession(**raw)
        if not engine_lab.session_needs_refresh(auth_session):
            return auth_session
        settings = load_supabase_settings()
        refreshed = engine_lab.refresh_session(http, settings, auth_session.refresh_token)
        if refreshed is None:
            session.pop(_SESSION_KEY, None)
            return None
        _store_session(refreshed)
        return refreshed

    def _require_reviewer():
        """Returns `(collaborator, session_client)`, or `(None, redirect)`."""
        auth_session = _current_auth_session()
        if auth_session is None:
            return None, redirect(url_for("engine_lab_login"))
        settings = load_supabase_settings()
        client = engine_lab.session_client(http, settings, auth_session)
        collaborator = engine_lab.get_own_collaborator(client, auth_session.user_id)
        if collaborator is None:
            session.pop(_SESSION_KEY, None)
            return None, redirect(url_for("engine_lab_login"))
        return (collaborator, client), None

    @app.get("/engine-lab/login")
    def engine_lab_login():
        return _page(
            "Engine Lab login",
            "<h1>Engine Lab</h1>"
            '<form method="post" action="/engine-lab/login">'
            '<label>Email<br><input type="email" name="email" autofocus required></label>'
            '<p><button type="submit">Send me a code</button></p>'
            "</form>",
        )

    @app.post("/engine-lab/login")
    def engine_lab_login_submit():
        email = (request.form.get("email") or "").strip()
        if not email:
            return _page("Engine Lab login", "<p>Enter an email address.</p>"), 400
        settings = load_supabase_settings()
        try:
            engine_lab.send_login_code(http, settings, email)
        except engine_lab.LoginError as exc:
            logger.warning("engine lab: failed to send login code: %s", exc)
            return _page("Engine Lab login", "<p>Could not send a code. Try again shortly.</p>"), 502
        session[_PENDING_EMAIL_KEY] = email
        return redirect(url_for("engine_lab_login_code"))

    @app.get("/engine-lab/login/code")
    def engine_lab_login_code():
        email = session.get(_PENDING_EMAIL_KEY)
        if not email:
            return redirect(url_for("engine_lab_login"))
        return _page(
            "Engine Lab login",
            f"<h1>Check {escape(email)}</h1><p>Enter the code we just emailed you.</p>"
            '<form method="post" action="/engine-lab/login/code">'
            '<label>Code<br><input type="text" name="code" inputmode="numeric" autofocus required></label>'
            '<p><button type="submit">Sign in</button></p>'
            "</form>",
        )

    @app.post("/engine-lab/login/code")
    def engine_lab_login_code_submit():
        email = session.get(_PENDING_EMAIL_KEY)
        code = (request.form.get("code") or "").strip()
        if not email or not code:
            return redirect(url_for("engine_lab_login"))

        settings = load_supabase_settings()
        try:
            auth_session = engine_lab.verify_login_code(http, settings, email, code)
        except engine_lab.LoginError as exc:
            return _page(
                "Engine Lab login",
                f"<p>That code didn't work: {escape(str(exc))}</p>"
                '<p><a href="/engine-lab/login">Start over</a></p>',
            ), 401

        session.pop(_PENDING_EMAIL_KEY, None)
        client = engine_lab.session_client(http, settings, auth_session)
        engine_lab.bootstrap_owner_if_matching(
            client, verified_email=auth_session.email, owner_email=_owner_email()
        )
        claimed = engine_lab.claim_invite(client)
        collaborator = engine_lab.get_own_collaborator(client, auth_session.user_id)
        if not claimed and collaborator is None:
            return _page(
                "Engine Lab login",
                "<p>That email hasn't been invited to Engine Lab.</p>",
            ), 403

        _store_session(auth_session)
        return redirect(url_for("engine_lab_review"))

    @app.get("/engine-lab/logout")
    def engine_lab_logout():
        session.pop(_SESSION_KEY, None)
        session.pop(_PENDING_EMAIL_KEY, None)
        return redirect(url_for("engine_lab_login"))

    def _nav(collaborator: engine_lab.Collaborator) -> str:
        links = ['<a href="/engine-lab/review">Review</a>']
        if collaborator.is_owner:
            links.append('<a href="/engine-lab/invite">Invite</a>')
            links.append('<a href="/engine-lab/summary">Summary</a>')
        links.append('<a href="/engine-lab/logout">Sign out</a>')
        return "<p>" + " &middot; ".join(links) + "</p>"

    @app.get("/engine-lab/review")
    def engine_lab_review():
        result, response = _require_reviewer()
        if response is not None:
            return response
        collaborator, client = result

        settings = load_supabase_settings()
        store = PostgresJobStore(engine_lab.subject_store_client(http, settings))

        try:
            already_shown = engine_lab.shown_posting_ids_today(client, collaborator.user_id)
            card = engine_lab.select_next_card(
                store,
                client,
                reviewer_id=collaborator.user_id,
                already_shown_posting_ids=already_shown,
            )
        except engine_lab.EngineLabUnavailable as exc:
            return _page(
                "Engine Lab -- unavailable",
                _nav(collaborator) + f"<h1>No card available</h1><p>{escape(str(exc))}</p>",
            )

        safe_url = _safe_link(card.posting_url)
        link_html = (
            f'<p><a href="{escape(safe_url)}" target="_blank" rel="noopener">View posting</a></p>'
            if safe_url
            else ""
        )
        return _page(
            "Engine Lab review",
            _nav(collaborator)
            + f"<h1>{escape(card.posting_title)}</h1>"
            f"<p><strong>{escape(card.posting_company)}</strong> "
            f"&middot; {escape(card.posting_location)}</p>"
            f"<p>{escape(card.why_line)}</p>"
            f"{link_html}"
            '<form method="post" action="/engine-lab/judge">'
            f'<input type="hidden" name="impression_id" value="{escape(card.impression_id)}">'
            "<fieldset><legend>Is this worth applying to?</legend>"
            '<label><input type="radio" name="worth_applying" value="true" required> Worth applying</label><br>'
            '<label><input type="radio" name="worth_applying" value="false"> Not worth applying</label>'
            "</fieldset>"
            "<fieldset><legend>Was the why line helpful?</legend>"
            '<label><input type="radio" name="why_line_judgement" value="helpful" required> Helpful</label><br>'
            '<label><input type="radio" name="why_line_judgement" value="flawed"> Flawed</label>'
            "</fieldset>"
            '<label>Problem or rejection reason (optional)<br>'
            '<input type="text" name="problem_reason"></label>'
            '<p><button type="submit">Submit judgement</button></p>'
            "</form>",
        )

    @app.post("/engine-lab/judge")
    def engine_lab_judge():
        result, response = _require_reviewer()
        if response is not None:
            return response
        collaborator, client = result

        impression_id = request.form.get("impression_id", "")
        worth_applying_raw = request.form.get("worth_applying")
        why_line_judgement = request.form.get("why_line_judgement", "")
        problem_reason = request.form.get("problem_reason") or None
        if not impression_id or worth_applying_raw not in ("true", "false"):
            return _page("Engine Lab", "<p>Missing or invalid judgement fields.</p>"), 400

        try:
            engine_lab.record_judgement(
                client,
                reviewer_id=collaborator.user_id,
                impression_id=impression_id,
                worth_applying=(worth_applying_raw == "true"),
                why_line_judgement=why_line_judgement,
                problem_reason=problem_reason,
            )
        except ValueError as exc:
            return _page("Engine Lab", f"<p>{escape(str(exc))}</p>"), 400

        cohort = engine_lab.reveal_cohort(client, impression_id)
        return _page(
            "Engine Lab -- judged",
            _nav(collaborator)
            + f"<p>Recorded. This card was: <strong>{escape(cohort)}</strong></p>"
            '<p><a href="/engine-lab/review">Next card</a></p>',
        )

    @app.get("/engine-lab/invite")
    def engine_lab_invite():
        result, response = _require_reviewer()
        if response is not None:
            return response
        collaborator, client = result
        if not collaborator.is_owner:
            return _page("Engine Lab", "<p>Only the owner can invite a collaborator.</p>"), 403

        rows = "".join(
            f"<li>{escape(c.email)} -- {'owner' if c.is_owner else ('revoked' if c.revoked_at else 'active')}</li>"
            for c in engine_lab.list_collaborators(client)
        )
        return _page(
            "Engine Lab -- invite",
            _nav(collaborator)
            + "<h1>Invite a collaborator</h1>"
            '<form method="post" action="/engine-lab/invite">'
            '<label>Email<br><input type="email" name="email" required></label>'
            '<p><button type="submit">Invite</button></p>'
            "</form>"
            f"<h2>Invited so far</h2><ul>{rows}</ul>",
        )

    @app.post("/engine-lab/invite")
    def engine_lab_invite_submit():
        result, response = _require_reviewer()
        if response is not None:
            return response
        collaborator, client = result
        if not collaborator.is_owner:
            return _page("Engine Lab", "<p>Only the owner can invite a collaborator.</p>"), 403

        email = (request.form.get("email") or "").strip()
        if not email:
            return _page("Engine Lab", "<p>Enter an email address.</p>"), 400
        engine_lab.invite_collaborator(client, email)
        return redirect(url_for("engine_lab_invite"))

    @app.get("/engine-lab/summary")
    def engine_lab_summary():
        result, response = _require_reviewer()
        if response is not None:
            return response
        collaborator, client = result
        if not collaborator.is_owner:
            return _page("Engine Lab", "<p>Only the owner can see the summary.</p>"), 403

        day_param = request.args.get("date")
        try:
            day = date.fromisoformat(day_param) if day_param else datetime.now(timezone.utc).date()
        except ValueError:
            return _page("Engine Lab summary", "<p>Invalid date.</p>"), 400

        rows = engine_lab.daily_summary(client, day)
        missing = [row.cohort for row in rows if row.impressions == 0]

        table_rows = "".join(
            "<tr>"
            f"<td>{escape(row.cohort)}</td>"
            f"<td>{row.impressions}</td>"
            f"<td>{row.judged}</td>"
            f"<td>{'--' if row.worth_applying_rate is None else f'{row.worth_applying_rate:.0%}'}</td>"
            f"<td>{'--' if row.helpful_rate is None else f'{row.helpful_rate:.0%}'}</td>"
            "</tr>"
            for row in rows
        )
        missing_notice = (
            f"<p><strong>Missing cohort(s):</strong> {escape(', '.join(missing))}</p>"
            if missing
            else ""
        )
        return _page(
            "Engine Lab summary",
            _nav(collaborator)
            + f"<h1>Summary for {escape(day.isoformat())}</h1>"
            f"{missing_notice}"
            "<table border=\"1\" cellpadding=\"6\">"
            "<tr><th>Cohort</th><th>Impressions</th><th>Judged</th>"
            "<th>Worth applying</th><th>Helpful why line</th></tr>"
            f"{table_rows}"
            "</table>",
        )

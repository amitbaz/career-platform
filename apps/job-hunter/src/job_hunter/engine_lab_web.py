"""HTTP routes for the private Engine Lab review page (#257).

Registered onto the same Flask app the Telegram webhook already uses
(`telegram_webhook.create_app`) -- see the design doc's "Where this lives"
section for why this is one Vercel function rather than a page in
`apps/relay`.

This module renders HTML and turns a request into a call into `engine_lab.py`.
It contains no selection, ranking or explanation logic of its own -- every
decision about which posting is shown, what its why line says, or which
cohort it belongs to is made by that module, never here.

All posting/evaluation text rendered on these pages came from a scraped job
advertisement or a model's own output -- untrusted content -- so every such
value is passed through `markupsafe.escape` before it reaches an HTML string.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime

from flask import Flask, redirect, request, session, url_for
from markupsafe import escape

from job_hunter import engine_lab
from job_hunter.config import load_supabase_settings
from job_hunter.http import HttpClient
from job_hunter.postgres_store import PostgresJobStore

logger = logging.getLogger(__name__)

_SESSION_KEY = "engine_lab_reviewer_id"


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
    `ENGINE_LAB_SESSION_SECRET` is unset. A request that actually reaches
    a session-backed route with no secret key fails there, loudly, which is
    the correct behaviour for a misconfigured deployment rather than a
    silent bypass of the login check.
    """
    if not app.secret_key:
        app.secret_key = os.environ.get("ENGINE_LAB_SESSION_SECRET")
    # Defense in depth alongside modern browsers' own default (unset
    # SameSite is already treated as Lax): a reviewer's session cookie
    # must never ride along on a cross-site request, and this deployment
    # is HTTPS-only, so there is no reason not to require it.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = True

    def _runner_client():
        settings = load_supabase_settings()
        return settings, engine_lab.runner_client(http, settings)

    def _current_reviewer():
        reviewer_id = session.get(_SESSION_KEY)
        if not reviewer_id:
            return None
        _, runner = _runner_client()
        return engine_lab.get_active_collaborator(runner, reviewer_id)

    def _require_reviewer():
        reviewer = _current_reviewer()
        if reviewer is None:
            session.pop(_SESSION_KEY, None)
            return None, redirect(url_for("engine_lab_login"))
        return reviewer, None

    @app.get("/engine-lab/login")
    def engine_lab_login():
        return _page(
            "Engine Lab login",
            '<h1>Engine Lab</h1>'
            '<form method="post" action="/engine-lab/login">'
            '<label>Invite token<br><input type="password" name="token" autofocus></label>'
            '<p><button type="submit">Sign in</button></p>'
            "</form>",
        )

    @app.post("/engine-lab/login")
    def engine_lab_login_submit():
        token = request.form.get("token", "")
        _, runner = _runner_client()
        collaborator = engine_lab.find_collaborator_by_token(runner, token) if token else None
        if collaborator is None:
            return _page(
                "Engine Lab login",
                "<p>That token is not recognised or has been revoked.</p>"
                '<p><a href="/engine-lab/login">Try again</a></p>',
            ), 401
        session[_SESSION_KEY] = collaborator.user_id
        return redirect(url_for("engine_lab_review"))

    @app.get("/engine-lab/logout")
    def engine_lab_logout():
        session.pop(_SESSION_KEY, None)
        return redirect(url_for("engine_lab_login"))

    @app.get("/engine-lab/review")
    def engine_lab_review():
        reviewer, response = _require_reviewer()
        if response is not None:
            return response

        settings, _ = _runner_client()
        store = PostgresJobStore(engine_lab.runner_client(http, settings))
        reviewer_conn = engine_lab.reviewer_client(http, settings, reviewer.user_id)

        try:
            already_shown = engine_lab.shown_posting_ids_today(reviewer_conn, reviewer.user_id)
            card = engine_lab.select_next_card(
                store,
                reviewer_conn,
                reviewer_id=reviewer.user_id,
                already_shown_posting_ids=already_shown,
            )
        except engine_lab.EngineLabUnavailable as exc:
            return _page(
                "Engine Lab -- unavailable",
                f"<h1>No card available</h1><p>{escape(str(exc))}</p>"
                f'<p><a href="/engine-lab/summary">Today\'s summary</a></p>',
            )

        safe_url = _safe_link(card.posting_url)
        link_html = (
            f'<p><a href="{escape(safe_url)}" target="_blank" rel="noopener">View posting</a></p>'
            if safe_url
            else ""
        )
        return _page(
            "Engine Lab review",
            f"<h1>{escape(card.posting_title)}</h1>"
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
        reviewer, response = _require_reviewer()
        if response is not None:
            return response

        impression_id = request.form.get("impression_id", "")
        worth_applying_raw = request.form.get("worth_applying")
        why_line_judgement = request.form.get("why_line_judgement", "")
        problem_reason = request.form.get("problem_reason") or None
        if not impression_id or worth_applying_raw not in ("true", "false"):
            return _page("Engine Lab", "<p>Missing or invalid judgement fields.</p>"), 400

        settings, runner = _runner_client()
        reviewer_conn = engine_lab.reviewer_client(http, settings, reviewer.user_id)
        try:
            engine_lab.record_judgement(
                reviewer_conn,
                reviewer_id=reviewer.user_id,
                impression_id=impression_id,
                worth_applying=(worth_applying_raw == "true"),
                why_line_judgement=why_line_judgement,
                problem_reason=problem_reason,
            )
        except ValueError as exc:
            return _page("Engine Lab", f"<p>{escape(str(exc))}</p>"), 400

        cohort = engine_lab.reveal_cohort(runner, impression_id)
        return _page(
            "Engine Lab -- judged",
            f"<p>Recorded. This card was: <strong>{escape(cohort)}</strong></p>"
            '<p><a href="/engine-lab/review">Next card</a></p>',
        )

    @app.get("/engine-lab/summary")
    def engine_lab_summary():
        _, response = _require_reviewer()
        if response is not None:
            return response

        day_param = request.args.get("date")
        try:
            day = date.fromisoformat(day_param) if day_param else datetime.utcnow().date()
        except ValueError:
            return _page("Engine Lab summary", "<p>Invalid date.</p>"), 400

        _, runner = _runner_client()
        rows = engine_lab.daily_summary(runner, day)
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
            f"<h1>Summary for {escape(day.isoformat())}</h1>"
            f"{missing_notice}"
            "<table border=\"1\" cellpadding=\"6\">"
            "<tr><th>Cohort</th><th>Impressions</th><th>Judged</th>"
            "<th>Worth applying</th><th>Helpful why line</th></tr>"
            f"{table_rows}"
            "</table>",
        )

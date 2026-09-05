from pathlib import Path

import pytest

from job_hunter.models import DigestItem, GeminiUsageSummary, ReviewItem
from job_hunter.telegram import (
    TelegramClient,
    build_digest,
    build_gemini_pause_warning,
    build_gmail_review_digest,
    build_gmail_review_digest_chunks,
    chunk_message,
    select_deliverable_items,
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


class FakeHttp:
    def __init__(self, response=None):
        self.response = response or FakeResponse(200, {"result": {"message_id": 42}})
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _item(**overrides):
    defaults = dict(
        job_id=1,
        company="Acme",
        title="Senior Product Engineer",
        score=90,
        decision="high_priority",
        url="https://acme.example/jobs/1",
        hard_blockers=[],
    )
    defaults.update(overrides)
    return DigestItem(**defaults)


def _usage_summary(**overrides):
    defaults = dict(
        requests_today=21,
        rpd_percent=34.0,
        rpm_peak_percent=20.0,
        tpm_peak_percent=17.0,
        # cached_tokens_today is a subset of input_tokens_today (Google's
        # cachedContentTokenCount is part of promptTokenCount, not additional
        # to it), so total_tokens_today is input+output+thinking (142k), not
        # input+output+thinking+cached.
        input_tokens_today=102_000,
        output_tokens_today=30_000,
        thinking_tokens_today=10_000,
        cached_tokens_today=2_000,
        total_tokens_today=142_000,
        purpose_counts={"job_evaluation": 21},
        internal_budget_exhausted=False,
        provider_paused=False,
    )
    defaults.update(overrides)
    return GeminiUsageSummary(**defaults)


def _review_item(**overrides):
    defaults = dict(
        event_id=1,
        company="Acme",
        role_title="Frontend Engineer",
        occurred_at="2026-08-31T10:00:00+00:00",
        subject="Interview details inside",
        rationale="ambiguous scheduling language",
        event_type="RECRUITER_CONTACT",
        source_message_id="gmail-message-1",
        source_thread_id="gmail-thread-1",
    )
    defaults.update(overrides)
    return ReviewItem(**defaults)


def test_chunk_message_splits_long_text_under_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = chunk_message(text, limit=3900)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 3900


def test_chunk_message_returns_single_chunk_for_short_text():
    assert chunk_message("hello") == ["hello"]


def test_build_digest_groups_by_decision():
    items = [
        _item(decision="high_priority", company="Acme", score=90),
        _item(decision="package_match", company="Beta", score=80),
        _item(decision="possible_match", company="Gamma", score=70),
        _item(decision="blocked", company="Delta", score=61, hard_blockers=["visa"]),
    ]
    digest = build_digest(items)
    assert "Ready to apply" in digest
    assert "Possible matches" in digest
    assert "Needs review / blockers" in digest
    assert digest.index("Ready to apply") < digest.index("Acme")
    assert digest.index("Ready to apply") < digest.index("Beta")
    assert digest.index("Possible matches") < digest.index("Gamma")
    assert digest.index("Needs review / blockers") < digest.index("Delta")
    assert "90" in digest and "Senior Product Engineer" in digest
    assert "https://acme.example/jobs/1" in digest


def test_build_digest_empty_items_returns_placeholder():
    digest = build_digest([])
    assert digest


def test_build_digest_omits_scores_at_or_below_sixty():
    digest = build_digest([
        _item(company="Keep", score=61, decision="possible_match"),
        _item(company="Drop60", score=60, decision="possible_match"),
        _item(company="DropLow", score=40, decision="blocked"),
    ])
    assert "Keep" in digest
    assert "Drop60" not in digest
    assert "DropLow" not in digest


def test_select_deliverable_items_keeps_only_scores_above_sixty():
    selected = select_deliverable_items([
        _item(company="Keep", score=61, decision="high_priority"),
        _item(company="KeepPossible", score=61, decision="possible_match"),
        _item(company="DropReady", score=60, decision="high_priority"),
        _item(company="DropPossible", score=60, decision="possible_match"),
    ])
    assert [item.company for item in selected] == ["Keep", "KeepPossible"]


def test_build_gmail_review_digest_uses_user_facing_activity_copy():
    digest = build_gmail_review_digest(
        [
            _review_item(
                company="Montash",
                role_title="Senior Frontend Engineer",
                rationale="deterministic recruiter template",
            )
        ]
    )

    assert digest == (
        "Gmail activity I couldn't link\n\n"
        "Montash — Senior Frontend Engineer\n"
        "A recruiter contacted you, but I couldn't link this email to a tracked job.\n"
        "Open email: https://mail.google.com/mail/u/0/#all/gmail-thread-1"
    )
    assert "deterministic recruiter template" not in digest


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("APPLIED", "This looks like an application confirmation, but I couldn't link it to a tracked job."),
        ("INTERVIEW", "This looks like an interview email, but I couldn't link it to a tracked job."),
        ("TECHNICAL", "This looks like a technical assessment, but I couldn't link it to a tracked job."),
        ("OFFER", "This looks like a job offer, but I couldn't link it to a tracked job."),
        ("REJECTED", "This looks like a rejection email, but I couldn't link it to a tracked job."),
        ("REVIEW_NEEDED", "This looks job-related, but I couldn't classify or link it confidently."),
        ("SOMETHING_LEGACY", "This looks job-related, but I couldn't classify or link it confidently."),
    ],
)
def test_gmail_activity_copy_describes_original_event_type(event_type, expected):
    digest = build_gmail_review_digest([_review_item(event_type=event_type)])

    assert expected in digest
    assert "ambiguous scheduling language" not in digest


def test_gmail_activity_identity_falls_back_to_company_event_label_then_subject():
    digest = build_gmail_review_digest(
        [
            _review_item(
                event_id=1,
                company="Supabase",
                role_title="",
                event_type="REVIEW_NEEDED",
                subject="Supabase product update",
                source_message_id="m-company",
                source_thread_id="thread-m-company",
            ),
            _review_item(
                event_id=2,
                company="",
                role_title="",
                event_type="INTERVIEW",
                subject="Interview details inside",
                source_message_id="m-subject",
                source_thread_id="thread-m-subject",
                occurred_at="2026-08-31T11:00:00+00:00",
            ),
        ]
    )

    assert "Supabase — Job-related activity" in digest
    assert "Interview details inside" in digest
    assert "https://mail.google.com/mail/u/0/#all/thread-m-company" in digest
    assert "https://mail.google.com/mail/u/0/#all/thread-m-subject" in digest


def test_gmail_activity_digest_sorts_events_by_time_then_id():
    digest = build_gmail_review_digest(
        [
            _review_item(event_id=4, company="Beta", occurred_at="2026-08-31T11:00:00+00:00", source_message_id="m4"),
            _review_item(event_id=3, company="Gamma", occurred_at="2026-08-31T10:00:00+00:00", source_message_id="m3"),
            _review_item(event_id=2, company="Acme", occurred_at="2026-08-31T10:00:00+00:00", source_message_id="m2"),
        ]
    )

    assert digest.index("Acme — Frontend Engineer") < digest.index("Gamma — Frontend Engineer")
    assert digest.index("Gamma — Frontend Engineer") < digest.index("Beta — Frontend Engineer")


def test_gmail_activity_chunking_keeps_each_event_atomic():
    items = [
        _review_item(
            event_id=index,
            company=f"Company {index}",
            source_message_id=f"m-{index}",
            source_thread_id=f"thread-m-{index}",
            rationale="internal detail that must not be shown",
        )
        for index in range(1, 8)
    ]

    chunks = build_gmail_review_digest_chunks(items, limit=420)

    assert len(chunks) > 1
    delivered_ids = []
    for text, event_ids in chunks:
        assert len(text) <= 420
        assert text.startswith("Gmail activity I couldn't link")
        assert "internal detail that must not be shown" not in text
        delivered_ids.extend(event_ids)
        for event_id in event_ids:
            assert f"Company {event_id} — Frontend Engineer" in text
            assert f"https://mail.google.com/mail/u/0/#all/thread-m-{event_id}" in text
    assert delivered_ids == list(range(1, 8))


def test_send_message_posts_to_send_message_endpoint():
    http = FakeHttp()
    client = TelegramClient("token123", "chat456", http)

    result = client.send_message("hello world")

    assert result == "42"
    url, kwargs = http.calls[0]
    assert url == "https://api.telegram.org/bottoken123/sendMessage"
    assert kwargs["json"]["chat_id"] == "chat456"
    assert kwargs["json"]["text"] == "hello world"


def test_send_message_chunks_long_text_into_multiple_calls():
    http = FakeHttp()
    client = TelegramClient("token123", "chat456", http)
    long_text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))

    client.send_message(long_text)

    assert len(http.calls) > 1
    for url, kwargs in http.calls:
        assert url.endswith("/sendMessage")
        assert kwargs["json"]["chat_id"] == "chat456"


def test_send_message_returns_none_on_failure():
    http = FakeHttp(FakeResponse(400, {}, "bad request"))
    client = TelegramClient("token123", "chat456", http)

    assert client.send_message("hello") is None


def test_send_document_posts_multipart_with_chat_id(tmp_path):
    doc_path = tmp_path / "letter.pdf"
    doc_path.write_bytes(b"%PDF-1.4 fake")
    http = FakeHttp()
    client = TelegramClient("token123", "chat456", http)

    result = client.send_document(doc_path, "Acme - Senior Product Engineer - 90 - https://acme.example/jobs/1")

    assert result == "42"
    url, kwargs = http.calls[0]
    assert url == "https://api.telegram.org/bottoken123/sendDocument"
    assert kwargs["data"]["chat_id"] == "chat456"
    assert "document" in kwargs["files"]
    assert kwargs["data"]["caption"].startswith("Acme - Senior Product Engineer")


def test_send_document_truncates_long_caption(tmp_path):
    doc_path = tmp_path / "letter.pdf"
    doc_path.write_bytes(b"%PDF-1.4 fake")
    http = FakeHttp()
    client = TelegramClient("token123", "chat456", http)

    client.send_document(doc_path, "x" * 2000)

    _, kwargs = http.calls[0]
    assert len(kwargs["data"]["caption"]) < 1024


def test_send_document_sends_bytes_not_open_file_handle(tmp_path):
    doc_path = tmp_path / "letter.pdf"
    doc_path.write_bytes(b"%PDF-1.4 fake content")
    http = FakeHttp()
    client = TelegramClient("token123", "chat456", http)

    client.send_document(doc_path, "caption")

    _, kwargs = http.calls[0]
    filename, content = kwargs["files"]["document"]
    assert filename == "letter.pdf"
    assert isinstance(content, bytes)
    assert content == b"%PDF-1.4 fake content"


def test_send_document_returns_none_on_failure(tmp_path):
    doc_path = tmp_path / "letter.pdf"
    doc_path.write_bytes(b"%PDF-1.4 fake")
    http = FakeHttp(FakeResponse(500, {}, "server error"))
    client = TelegramClient("token123", "chat456", http)

    assert client.send_document(doc_path, "caption") is None


def test_build_gemini_pause_warning_returns_none_when_healthy():
    assert build_gemini_pause_warning(_usage_summary()) is None


def test_build_gemini_pause_warning_when_provider_paused():
    warning = build_gemini_pause_warning(_usage_summary(provider_paused=True))

    assert warning is not None
    assert "paused" in warning.lower()


def test_build_gemini_pause_warning_when_internal_budget_exhausted():
    warning = build_gemini_pause_warning(_usage_summary(internal_budget_exhausted=True))

    assert warning is not None
    assert "deferred" in warning.lower()

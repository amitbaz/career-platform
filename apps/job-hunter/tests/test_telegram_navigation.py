import pytest

from job_hunter.models import NavigationCard
from job_hunter.telegram_navigation import (
    build_navigation_card,
    encode_callback,
    navigation_sort_key,
    parse_callback,
)
from job_hunter.models import DigestItem


def _card(**overrides):
    data = dict(
        job_id=7,
        title="Senior Frontend Developer",
        company="Example GmbH",
        location="Berlin",
        score=87,
        url="https://example.test/jobs/7",
    )
    data.update(overrides)
    return NavigationCard(**data)


def test_build_navigation_card_middle_item():
    text, keyboard = build_navigation_card(_card(), "abc123", index=2, total=12)
    assert text == (
        "Senior Frontend Developer\n\n"
        "Company: Example GmbH\n"
        "Location: Berlin\n"
        "Match: 87%"
    )
    assert keyboard[0][0] == {"text": "View job", "url": "https://example.test/jobs/7"}
    assert keyboard[0][1]["text"] == "Apply"
    assert keyboard[1][0]["text"] == "◀ Previous"
    assert parse_callback(keyboard[1][0]["callback_data"]) == ("n", "abc123", 1)
    assert keyboard[1][1]["text"] == "3 / 12"
    assert parse_callback(keyboard[1][1]["callback_data"]) == ("x", "abc123", 2)
    assert keyboard[1][2]["text"] == "Next ▶"
    assert parse_callback(keyboard[1][2]["callback_data"]) == ("n", "abc123", 3)


def test_build_navigation_card_first_and_last_do_not_wrap():
    _, first_keyboard = build_navigation_card(_card(), "s", index=0, total=3)
    assert parse_callback(first_keyboard[1][0]["callback_data"]) == ("x", "s", 0)
    assert parse_callback(first_keyboard[1][2]["callback_data"]) == ("n", "s", 1)

    _, last_keyboard = build_navigation_card(_card(), "s", index=2, total=3)
    assert parse_callback(last_keyboard[1][0]["callback_data"]) == ("n", "s", 1)
    assert parse_callback(last_keyboard[1][2]["callback_data"]) == ("x", "s", 2)


def test_build_navigation_card_fallbacks_and_missing_url():
    text, keyboard = build_navigation_card(
        _card(company="", location="", url=""), "s", index=0, total=1
    )
    assert "Company: Not specified" in text
    assert "Location: Not specified" in text
    assert keyboard[0] == [
        {"text": "Apply", "callback_data": "a|s|0"},
        {"text": "Gen CL", "callback_data": "c|s|0"},
    ]


def test_build_navigation_card_includes_gen_cl_button():
    text, keyboard = build_navigation_card(_card(), "abc123", index=2, total=12)
    assert keyboard[0][2] == {"text": "Gen CL", "callback_data": "c|abc123|2"}


def test_navigation_card_surfaces_market_note():
    card = NavigationCard(
        job_id=1,
        title="Senior Frontend Engineer",
        company="Acme",
        location="London",
        score=91,
        url="https://example.test/job",
        market_id="london",
        market_note="Visa sponsorship: unknown. Requires 4 hours overlap with EST.",
    )
    text, _ = build_navigation_card(card, "session", 0, 1)
    assert "Visa sponsorship: unknown" in text
    assert "4 hours overlap with EST" in text


def test_callback_round_trip_and_size_limit():
    payload = encode_callback("n", "abc123", 11)
    assert parse_callback(payload) == ("n", "abc123", 11)
    assert len(payload.encode("utf-8")) <= 64


@pytest.mark.parametrize(
    "payload",
    ["", "n", "n||0", "n|s|-1", "z|s|0", "n|s|abc", "n|s|0|extra"],
)
def test_parse_callback_rejects_malformed_payload(payload):
    assert parse_callback(payload) is None


def test_encode_callback_rejects_invalid_or_oversized_payload():
    with pytest.raises(ValueError):
        encode_callback("z", "s", 0)
    with pytest.raises(ValueError):
        encode_callback("n", "", 0)
    with pytest.raises(ValueError):
        encode_callback("n", "s", -1)
    with pytest.raises(ValueError):
        encode_callback("n", "x" * 70, 0)


def test_navigation_sort_key_is_score_then_company_title_job_id():
    items = [
        DigestItem(3, "Beta", "Senior B", 90, "high_priority", "u", [], location="Remote"),
        DigestItem(2, "Acme", "Senior Z", 90, "high_priority", "u", [], location="Berlin"),
        DigestItem(1, "Acme", "Senior A", 90, "high_priority", "u", [], location="Berlin"),
        DigestItem(4, "Acme", "Senior A", 80, "high_priority", "u", [], location="Berlin"),
    ]
    assert [item.job_id for item in sorted(items, key=navigation_sort_key)] == [1, 2, 3, 4]

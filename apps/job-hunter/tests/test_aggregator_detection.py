from job_hunter.aggregator_detection import evaluate_board, third_party_listing

_JOBGETHER_PHRASING = (
    "This position is listed on behalf of a partner company, who manages "
    "all applications and next steps."
)


def _postings(count: int, phrasing: str = "") -> list[str]:
    return [f"Great role. {phrasing} Apply now." for _ in range(count)]


def test_third_party_listing_fires_when_majority_of_postings_declare_third_party():
    descriptions = _postings(2) + _postings(6, _JOBGETHER_PHRASING)

    evidence = third_party_listing(descriptions)

    assert evidence.fired is True
    assert evidence.matched == 6
    assert evidence.scanned == 8


def test_third_party_listing_does_not_fire_for_employer_board_with_no_markers():
    descriptions = _postings(20)

    evidence = third_party_listing(descriptions)

    assert evidence.fired is False
    assert evidence.matched == 0


def test_third_party_listing_survives_one_stray_posting():
    descriptions = _postings(19) + _postings(1, _JOBGETHER_PHRASING)

    evidence = third_party_listing(descriptions)

    assert evidence.fired is False
    assert evidence.matched == 1


def test_third_party_listing_does_not_fire_below_minimum_sample_size():
    # 2 of 2 postings match, but a two-posting sample is too small to trust.
    descriptions = _postings(2, _JOBGETHER_PHRASING)

    evidence = third_party_listing(descriptions)

    assert evidence.fired is False


def test_third_party_listing_ignores_generic_on_behalf_of_phrasing():
    # "on behalf of" alone is the false-positive-prone phrase (e.g. recruiting
    # scam warnings inside legitimate postings) -- only the specific
    # third-party-listing phrasing should count.
    descriptions = _postings(
        6, "Beware of recruiters contacting you on behalf of this company."
    )

    evidence = third_party_listing(descriptions)

    assert evidence.fired is False
    assert evidence.matched == 0


def test_evaluate_board_rejects_jobgether_shaped_board_with_reason():
    descriptions = _postings(3) + _postings(97, _JOBGETHER_PHRASING)

    verdict = evaluate_board(descriptions)

    assert verdict.rejected is True
    assert "third_party_listing" in verdict.reason


def test_evaluate_board_accepts_veeva_shaped_board_with_no_markers():
    descriptions = _postings(21)

    verdict = evaluate_board(descriptions)

    assert verdict.rejected is False
    assert verdict.reason is None

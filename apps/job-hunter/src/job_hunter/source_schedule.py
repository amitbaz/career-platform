"""The per-source crawl cadence, derived from measured novelty (issue #184).

Kept apart from the scheduler that installs the cron entries so that the
policy -- how often a source is worth visiting -- is a pure function over
what the last crawl produced, and can be tested exhaustively without a
database.

No operator sets these frequencies. A source earns a faster band by
producing material the corpus did not already have, and loses one by
producing none, and that is the whole mechanism. Configuration may pin one
source as an override; it is never how a cadence is arrived at.
"""

from __future__ import annotations

import hashlib

#: Crawl cadence in minutes, fastest first. A source moves one step at a
#: time in either direction, so the ladder's spacing is the recovery rate as
#: much as it is the range: five empty crawls take the most-favoured source
#: to the floor, and five productive ones bring it back.
BANDS: tuple[int, ...] = (15, 60, 360, 1440, 4320, 10080)

_HEALTHY_OUTCOMES = frozenset({"fetched", "not_modified"})
_BACKOFF_OUTCOMES = frozenset({"rate_limited", "failed"})


def next_band(current_index: int, *, outcome: str, novelty: int) -> int:
    """Return the band a source moves to after one crawl.

    `not_modified` is healthy but produced nothing, so it demotes exactly
    like an empty fetch: a board that keeps answering "unchanged" is
    telling us it does not need visiting this often.

    `rate_limited` and `failed` demote regardless of what the crawl
    returned, because the constraint is the source's tolerance rather than
    its productivity -- and because a source that returns rows *and* a 429
    is precisely the one to slow down.
    """
    if outcome in _BACKOFF_OUTCOMES:
        return min(current_index + 1, len(BANDS) - 1)
    if outcome not in _HEALTHY_OUTCOMES:
        raise ValueError(f"unknown crawl outcome: {outcome!r}")
    if novelty > 0:
        return max(current_index - 1, 0)
    return min(current_index + 1, len(BANDS) - 1)


def _offset(source_key: str, modulus: int) -> int:
    """A stable per-source offset, so sources on one band do not stampede.

    Derived from the key rather than from a counter so it survives a source
    being removed and re-added, and so two deployments schedule the same
    source at the same minute.
    """
    digest = hashlib.sha256(source_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % modulus


def cron_expression(band_index: int, *, source_key: str) -> str:
    """Render one band as a five-field pg_cron expression.

    The 72-hour band uses a day-of-month step, which pg_cron restarts each
    month: days 1, 4, ... 31 then 1 gives one shortened gap at a month
    boundary. That is accepted rather than worked around -- a crawl arriving
    a day early once a month costs one extra conditional request.
    """
    if not 0 <= band_index < len(BANDS):
        raise ValueError(f"band index out of range: {band_index}")
    minute = _offset(source_key, 60)
    hour = _offset(source_key + ":hour", 24)
    minutes = BANDS[band_index]
    if minutes == 15:
        return f"{minute % 15}-59/15 * * * *"
    if minutes == 60:
        return f"{minute} * * * *"
    if minutes == 360:
        return f"{minute} {hour % 6}-23/6 * * *"
    if minutes == 1440:
        return f"{minute} {hour} * * *"
    if minutes == 4320:
        return f"{minute} {hour} 1-31/3 * *"
    return f"{minute} {hour} * * {_offset(source_key + ':dow', 7)}"

"""The per-source crawl cadence, derived from measured novelty (issue #184).

This module renders a band index to a cron expression. It does not decide
which band a source is in -- `job_hunter_reschedule_sources()` in the
migration does, from the last six crawls, and pgTAP asserts it there. The
split is deliberate: the decision needs crawl history and therefore a
database, while the rendering does not, and `BANDS` plus the cron shapes are
cross-checked against the migration from the Python side.

No operator sets these frequencies. A source earns a faster band by
producing material the corpus did not already have, and loses one by
producing none, and that is the whole mechanism. Configuration may pin one
source as an override; it is never how a cadence is arrived at.
"""

from __future__ import annotations

import hashlib

#: Crawl cadence in minutes, fastest first. The scheduler bands a source at
#: `3 + demotions - promotions` over its last six crawls, so six barren
#: crawls take the most-favoured source to the floor and six productive ones
#: bring it back. The ladder's spacing is therefore the recovery rate as much
#: as it is the range.
BANDS: tuple[int, ...] = (15, 60, 360, 1440, 4320, 10080)

# Which band a source lands in is decided by job_hunter_reschedule_sources()
# in the migration, not here. This module renders a band index to a cron
# expression and nothing more. A second decision procedure lived here until
# it was removed: it stepped one band from the source's current one, while
# the SQL computes an absolute band from the last six crawls, so the two
# disagreed about every history -- and only the SQL ever ran. If band
# selection needs testing, test it where it happens (pgTAP,
# job_hunter_source_registry.sql).

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

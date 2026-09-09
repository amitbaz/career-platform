"""The privileged, platform-funded ``extract_facets`` stage (issue #185).

Objective extraction becomes a queue consumer instead of a phase inside a
run. Postings needing facets -- never read, or read at a description that
has since changed -- are enqueued as a side effect of every job persist
(`PostgresJobStore._enqueue_needing_facets_for_job_ids`, called from
`upsert_job`, `upsert_logical_job` and `upsert_logical_jobs`) and drained
here at the rate the platform allowance can sustain. Extraction
remains the only stage that spends provider quota, so the platform key, its
pacing and its fail-closed rule (issue #128) live here and nowhere else: this
module never sees a user, only a posting id, and there is no argument
anything per-user could arrive through -- the same guarantee `facets.py`
makes about `extract_facets` itself.

Failure handling follows the queue's common vocabulary (`stage_queue.py`):

* Unparseable output (`FacetExtractionError`) says nothing usable about the
  posting, so it dead-letters immediately (`PermanentStageFailure`) rather
  than spending another call on the same non-answer.
* A vanished posting (merged away, or never real) also dead-letters
  immediately -- there is nothing left to read.
* An exhausted platform allowance, a paused provider, or a missing
  credential are none of the above: the posting was never read and nothing
  was spent, so the message returns to the queue without counting an attempt
  (`QuotaExhausted`), and a later run drains it.
* Anything else (a timeout, a transient provider 5xx) is an ordinary
  transient failure and retries with backoff -- `StageRunner`'s default for
  an exception that is not one of the above.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from job_hunter.ai import (
    AIQuotaPaused,
    AITemporaryCapacity,
    CredentialUnavailable,
    PlatformAllowanceExhausted,
)
from job_hunter.facets import FacetExtractionError, PostingFacts, extract_facets
from job_hunter.models import JobFacets
from job_hunter.stage_queue import (
    PermanentStageFailure,
    QueueMessage,
    QuotaExhausted,
    Stage,
    TransientStageFailure,
)

if TYPE_CHECKING:
    from job_hunter.ai import AIProvider

#: How long a paced-out message waits before this stage is asked again, for
#: the refusals that carry no provider-stated retry-after of their own
#: (an exhausted daily allowance, an active pause, no platform credential).
#: Chosen to roughly match how often a run drains this queue, not any
#: provider signal.
_ALLOWANCE_RETRY_DELAY_SECONDS = 15 * 60


class _ConnectionLease(Protocol):
    def connection(self): ...


@dataclass(frozen=True)
class ExtractedFacets:
    posting_id: str
    facets: JobFacets


@dataclass(frozen=True)
class FacetExtractionOutcome:
    """What draining one message from the queue did, for the run log.

    `parse_failure` is set only when the provider answered with something
    `extract_facets` could not read -- the same bucket `pipeline.py`'s inline
    reads count as `extraction_parse_failures`. A vanished posting or a
    storage failure is a failure but not a parse failure.
    """

    failed: bool
    parse_failure: bool = False


class ExtractFacetsStage:
    """Read one posting's objective facts and store them, once, for everyone."""

    def __init__(self, database: _ConnectionLease, ai: "AIProvider") -> None:
        self._database = database
        self._ai = ai

    def __call__(self, message: QueueMessage) -> ExtractedFacets:
        posting_id = self._posting_id(message)
        posting = self._read_posting(posting_id)
        if posting is None:
            raise PermanentStageFailure(f"posting_id={posting_id} no longer exists")

        try:
            facets = extract_facets(posting, self._ai)
        except PlatformAllowanceExhausted as error:
            raise QuotaExhausted(_ALLOWANCE_RETRY_DELAY_SECONDS) from error
        except AITemporaryCapacity as error:
            raise QuotaExhausted(max(0, int(error.retry_after_seconds))) from error
        except AIQuotaPaused as error:
            raise QuotaExhausted(_ALLOWANCE_RETRY_DELAY_SECONDS) from error
        except CredentialUnavailable as error:
            raise QuotaExhausted(_ALLOWANCE_RETRY_DELAY_SECONDS) from error
        except FacetExtractionError as error:
            raise PermanentStageFailure(str(error)) from error

        self._write_facets(posting_id, facets)
        return ExtractedFacets(posting_id=posting_id, facets=facets)

    @staticmethod
    def _posting_id(message: QueueMessage) -> str:
        if message.stage is not Stage.EXTRACT_FACETS:
            raise PermanentStageFailure("extract_facets received the wrong stage")
        if set(message.payload) != {"posting_id"}:
            raise PermanentStageFailure(
                "extract_facets payload must contain only posting_id"
            )
        posting_id = message.payload.get("posting_id")
        if not isinstance(posting_id, str):
            raise PermanentStageFailure("extract_facets posting_id must be a UUID")
        try:
            return str(uuid.UUID(posting_id))
        except ValueError as error:
            raise PermanentStageFailure(
                "extract_facets posting_id must be a UUID"
            ) from error

    def _read_posting(self, posting_id: str) -> PostingFacts | None:
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select title, company, location, remote, description, "
                        "content_confidence, source "
                        "from public.job_hunter_postings where id = %s",
                        (posting_id,),
                    )
                    row = cursor.fetchone()
        except Exception as error:
            raise TransientStageFailure("reading the posting failed") from error
        if row is None:
            return None
        (
            title,
            company,
            location,
            remote,
            description,
            content_confidence,
            source,
        ) = row
        return PostingFacts.from_posting_row(
            {
                "title": title,
                "company": company,
                "location": location,
                "remote": remote,
                "description": description,
                "content_confidence": content_confidence,
                "source": source,
            }
        )

    def _write_facets(self, posting_id: str, facets: JobFacets) -> None:
        compensation = facets.compensation
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "insert into public.job_hunter_job_facets "
                        "(posting_id, description_hash_at_extraction, seniority, "
                        "remote_policy, relocation_policy, hiring_regions, stack, "
                        "compensation_disclosed, compensation_currency, "
                        "compensation_min, compensation_max, compensation_period, "
                        "requirements_json, source_supplied, model, extracted_at) "
                        "select %s, description_hash, %s, %s, %s, %s, %s, %s, %s, "
                        "%s, %s, %s, %s::jsonb, %s, %s, now() "
                        "from public.job_hunter_postings where id = %s "
                        "on conflict (posting_id) do update set "
                        "description_hash_at_extraction = excluded.description_hash_at_extraction, "
                        "seniority = excluded.seniority, "
                        "remote_policy = excluded.remote_policy, "
                        "relocation_policy = excluded.relocation_policy, "
                        "hiring_regions = excluded.hiring_regions, "
                        "stack = excluded.stack, "
                        "compensation_disclosed = excluded.compensation_disclosed, "
                        "compensation_currency = excluded.compensation_currency, "
                        "compensation_min = excluded.compensation_min, "
                        "compensation_max = excluded.compensation_max, "
                        "compensation_period = excluded.compensation_period, "
                        "requirements_json = excluded.requirements_json, "
                        "source_supplied = excluded.source_supplied, "
                        "model = excluded.model, "
                        "extracted_at = excluded.extracted_at",
                        (
                            posting_id,
                            facets.seniority,
                            facets.remote_policy,
                            facets.relocation_policy,
                            facets.hiring_regions,
                            facets.stack,
                            compensation.disclosed,
                            compensation.currency,
                            compensation.minimum,
                            compensation.maximum,
                            compensation.period,
                            _to_jsonb(facets.requirements),
                            facets.source_supplied,
                            facets.model,
                            posting_id,
                        ),
                    )
        except Exception as error:
            raise TransientStageFailure("storing facets failed") from error


def _to_jsonb(value: object) -> str:
    return json.dumps(value)

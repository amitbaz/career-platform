from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

from job_hunter.gmail_classifier import (
    SemanticClassificationError,
    classify_email,
    source_candidate_key,
)
from job_hunter.gemini_usage import GeminiBudgetExceeded, GeminiQuotaPaused
from job_hunter.gmail_client import GmailHistoryExpired
from job_hunter.gmail_linkedin_cleanup import release_legacy_blank_linkedin_jobs
from job_hunter.gmail_matching import match_job
from job_hunter.gmail_models import (
    AUTO_CONFIDENCE_THRESHOLD,
    DISCOVERY_FRESHNESS_DAYS,
    MATCH_RECENCY_DAYS,
    GmailClassification,
    GmailMessage,
    GmailSyncSummary,
)


_LOGGER = logging.getLogger(__name__)
_QUERY_TERMS = (
    '{application interview recruiter hiring "job alert" position '
    '"technical assessment" "coding challenge" "job offer" '
    '"offer letter" "offer of employment" "pleased to offer you"}'
)
_LIFECYCLE_KINDS = frozenset(
    {"RECRUITER_CONTACT", "APPLIED", "INTERVIEW", "TECHNICAL", "OFFER", "REJECTED"}
)
DEFAULT_BACKFILL_BATCH_SIZE = 100


def _query_after(year: int, month: int, day: int) -> str:
    return f"after:{year:04d}/{month:02d}/{day:02d} {_QUERY_TERMS}"


def build_backfill_query(now: datetime) -> str:
    start = now - timedelta(days=MATCH_RECENCY_DAYS)
    return _query_after(start.year, start.month, start.day)


class GmailSyncService:
    def __init__(
        self,
        *,
        gmail,
        gemini,
        store,
        backfill_batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE,
    ) -> None:
        if backfill_batch_size <= 0:
            raise ValueError("backfill_batch_size must be positive")
        self.gmail = gmail
        self.gemini = gemini
        self.store = store
        self.backfill_batch_size = backfill_batch_size
        self._backfill_now: datetime | None = None

    def sync(
        self,
        now: datetime,
        dry_run: bool = False,
        force_backfill: bool = False,
    ) -> GmailSyncSummary:
        summary = GmailSyncSummary()
        account_id, checkpoint_history_id = self.gmail.get_profile()
        state = self.store.get_gmail_sync_state(account_id)

        if not dry_run:
            released_legacy = self.store.release_legacy_gmail_semantic_failures()
            released_linkedin = release_legacy_blank_linkedin_jobs(self.store)
            if released_legacy:
                _LOGGER.info(
                    "gmail_legacy_semantic_failures_released=%s", released_legacy
                )
            if released_linkedin:
                _LOGGER.info(
                    "gmail_legacy_blank_linkedin_jobs_released=%s", released_linkedin
                )
            if (
                released_legacy or released_linkedin
            ) and state is not None and state["backfill_completed_at"] is not None:
                self.store.save_gmail_sync_state(
                    account_id=account_id,
                    history_id=state["history_id"],
                    last_successful_sync_at=state["last_successful_sync_at"],
                    backfill_completed_at=None,
                )
                state = self.store.get_gmail_sync_state(account_id)

        backfill_pending = state is None or state["backfill_completed_at"] is None
        if force_backfill and state is not None and not dry_run:
            self.store.save_gmail_sync_state(
                account_id=account_id,
                history_id=state["history_id"],
                last_successful_sync_at=state["last_successful_sync_at"],
                backfill_completed_at=None,
            )

        if backfill_pending or force_backfill:
            message_ids = self._search_message_ids(build_backfill_query(now))
            self._backfill_now = now
            try:
                had_hard_errors, deferred_unprocessed, quota_paused = (
                    self._process_message_ids(
                        message_ids,
                        summary=summary,
                        dry_run=dry_run,
                        max_unprocessed=self.backfill_batch_size,
                    )
                )
                _LOGGER.info(
                    "gmail_backfill_batch candidates=%s batch_size=%s deferred=%s",
                    len(message_ids),
                    self.backfill_batch_size,
                    deferred_unprocessed,
                )
            finally:
                self._backfill_now = None
            if (
                not dry_run
                and not had_hard_errors
                and not quota_paused
                and deferred_unprocessed == 0
            ):
                self.store.save_gmail_sync_state(
                    account_id=account_id,
                    history_id=checkpoint_history_id,
                    last_successful_sync_at=now.isoformat(),
                    backfill_completed_at=now.isoformat(),
                )
        else:
            self._sync_incremental(
                state=state,
                checkpoint_history_id=checkpoint_history_id,
                now=now,
                dry_run=dry_run,
                summary=summary,
            )

        self._log_summary(summary)
        return summary

    def _sync_incremental(
        self,
        *,
        state,
        checkpoint_history_id: str,
        now: datetime,
        dry_run: bool,
        summary: GmailSyncSummary,
    ) -> None:
        saved_history_id = state["history_id"]
        recovered_from_expiry = False
        try:
            message_ids, next_history_id = self._list_history_message_ids(
                saved_history_id
            )
        except GmailHistoryExpired:
            recovered_from_expiry = True
            last_successful = (
                datetime.fromisoformat(state["last_successful_sync_at"])
                if state["last_successful_sync_at"]
                else now
            )
            overlap_start = last_successful - timedelta(days=1)
            message_ids = self._search_message_ids(
                _query_after(overlap_start.year, overlap_start.month, overlap_start.day)
            )
            next_history_id = checkpoint_history_id

        had_hard_errors, deferred_unprocessed, quota_paused = self._process_message_ids(
            message_ids,
            summary=summary,
            dry_run=dry_run,
        )
        assert deferred_unprocessed == 0
        if dry_run or had_hard_errors or quota_paused:
            return

        self.store.save_gmail_sync_state(
            account_id=state["account_id"],
            history_id=(checkpoint_history_id if recovered_from_expiry else next_history_id),
            last_successful_sync_at=now.isoformat(),
            backfill_completed_at=state["backfill_completed_at"],
        )

    def process_message(
        self, message: GmailMessage, dry_run: bool
    ) -> GmailClassification:
        # Freshness must be computed before classify_email decides whether to
        # request job-alert extraction: a gate applied only afterwards would
        # still have spent the Gemini call it's supposed to avoid.
        is_fresh = self._alert_is_fresh(message)
        classification = classify_email(message, self.gemini, is_fresh=is_fresh)
        effective = classification

        if dry_run:
            _LOGGER.info(
                "gmail_dry_run message_id=%s classification=%s extracted_jobs=%s",
                message.message_id,
                classification.kind,
                len(classification.jobs),
            )
            if classification.kind in _LIFECYCLE_KINDS:
                effective, _ = self._match_classification(message, classification)
            return effective

        should_stage_jobs = classification.kind == "RECRUITER_CONTACT" or (
            classification.kind == "JOB_ALERT" and is_fresh
        )
        if should_stage_jobs:
            for job in classification.jobs:
                self.store.stage_inbound_job(
                    message.message_id,
                    source_candidate_key(job),
                    job,
                )

        if classification.kind in _LIFECYCLE_KINDS:
            effective, job_id = self._match_classification(message, classification)
            self._save_event(message, classification, job_id)
        elif classification.kind == "REVIEW_NEEDED":
            self._save_event(message, classification, None)

        self.store.record_gmail_message(
            message_id=message.message_id,
            thread_id=message.thread_id,
            sender=message.sender,
            subject=message.subject,
            occurred_at=message.sent_at.isoformat(),
            classification=effective.kind,
            confidence=effective.confidence,
            rationale=effective.rationale,
        )
        return effective

    def _alert_is_fresh(self, message: GmailMessage) -> bool:
        if self._backfill_now is None:
            return True
        return self._backfill_now - message.sent_at <= timedelta(
            days=DISCOVERY_FRESHNESS_DAYS
        )

    def _match_classification(
        self,
        message: GmailMessage,
        classification: GmailClassification,
    ) -> tuple[GmailClassification, int | None]:
        match = match_job(self.store, classification, message)
        if match.job_id is None or match.ambiguous:
            return replace(classification, kind="REVIEW_NEEDED"), None
        if classification.confidence < AUTO_CONFIDENCE_THRESHOLD:
            return replace(classification, kind="REVIEW_NEEDED"), match.job_id
        return classification, match.job_id

    def _save_event(
        self,
        message: GmailMessage,
        classification: GmailClassification,
        job_id: int | None,
    ) -> None:
        self.store.save_application_event(
            job_id=job_id,
            event_type=classification.kind,
            occurred_at=message.sent_at.isoformat(),
            source_message_id=message.message_id,
            source_thread_id=message.thread_id,
            confidence=classification.confidence,
            company=classification.company,
            role_title=classification.role_title,
            rationale=classification.rationale,
        )

    def _search_message_ids(self, query: str) -> list[str]:
        message_ids: list[str] = []
        seen: set[str] = set()
        page_token = None
        while True:
            page = self.gmail.list_message_ids(query, page_token=page_token)
            for message_id in page.message_ids:
                if message_id not in seen:
                    seen.add(message_id)
                    message_ids.append(message_id)
            page_token = page.next_page_token
            if page_token is None:
                return message_ids

    def _list_history_message_ids(self, start_history_id: str) -> tuple[list[str], str]:
        message_ids: list[str] = []
        seen: set[str] = set()
        newest_history_id = start_history_id
        page_token = None
        while True:
            page = self.gmail.list_history(
                start_history_id,
                page_token=page_token,
            )
            newest_history_id = page.history_id
            for message_id in page.message_ids:
                if message_id not in seen:
                    seen.add(message_id)
                    message_ids.append(message_id)
            page_token = page.next_page_token
            if page_token is None:
                return message_ids, newest_history_id

    def _process_message_ids(
        self,
        message_ids: list[str],
        *,
        summary: GmailSyncSummary,
        dry_run: bool,
        max_unprocessed: int | None = None,
    ) -> tuple[bool, int, bool]:
        had_hard_errors = False
        quota_paused = False
        attempted_unprocessed = 0
        deferred_unprocessed = 0
        for message_id in message_ids:
            summary.fetched += 1
            if not dry_run:
                try:
                    if self.store.has_processed_gmail_message(message_id):
                        continue
                except Exception:
                    _LOGGER.exception(
                        "gmail_message_processing_failed message_id=%s", message_id
                    )
                    summary.errors += 1
                    had_hard_errors = True
                    continue

            if (
                max_unprocessed is not None
                and attempted_unprocessed >= max_unprocessed
            ):
                deferred_unprocessed += 1
                continue

            attempted_unprocessed += 1
            try:
                classification = self.process_message(
                    self.gmail.get_message(message_id),
                    dry_run=dry_run,
                )
            except (GeminiQuotaPaused, GeminiBudgetExceeded) as exc:
                # The circuit breaker is open (or our own ceiling is): stop
                # attempting further messages rather than hammering a paused
                # API. This message was never recorded, so it stays
                # unprocessed and will be retried on the next sync; the
                # cursor/backfill-completion gate below must not advance
                # past it either.
                _LOGGER.warning(
                    "gmail_quota_paused_stopping_batch message_id=%s reason=%s",
                    message_id,
                    exc,
                )
                quota_paused = True
                break
            except SemanticClassificationError as exc:
                _LOGGER.warning(
                    "gmail_semantic_classification_failed message_id=%s reason=%s detail=%s",
                    message_id,
                    exc.reason,
                    exc.detail or "unknown",
                )
                summary.errors += 1
                had_hard_errors = True
                continue
            except Exception:
                _LOGGER.exception("gmail_message_processing_failed message_id=%s", message_id)
                summary.errors += 1
                had_hard_errors = True
                continue

            summary.processed += 1
            if classification.kind == "JOB_ALERT":
                summary.job_alerts += 1
            elif classification.kind == "REVIEW_NEEDED":
                summary.review_needed += 1
            elif classification.kind == "IRRELEVANT":
                summary.irrelevant += 1
            elif classification.kind in _LIFECYCLE_KINDS:
                summary.application_events += 1
        return had_hard_errors, deferred_unprocessed, quota_paused

    @staticmethod
    def _log_summary(summary: GmailSyncSummary) -> None:
        _LOGGER.info(
            "gmail_fetched=%s gmail_processed=%s gmail_job_alerts=%s "
            "gmail_application_events=%s gmail_review_needed=%s "
            "gmail_irrelevant=%s gmail_errors=%s",
            summary.fetched,
            summary.processed,
            summary.job_alerts,
            summary.application_events,
            summary.review_needed,
            summary.irrelevant,
            summary.errors,
        )
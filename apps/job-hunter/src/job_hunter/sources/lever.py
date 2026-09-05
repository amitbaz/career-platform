from __future__ import annotations

from job_hunter.models import Job
from job_hunter.normalize import canonicalize_url

from .base import is_stale_board_error, logger, strip_html

_URL_TEMPLATE = "https://api.lever.co/v0/postings/{site}?mode=json"

_REMOTE_WORKPLACE_TYPES = {"remote"}
_ONSITE_WORKPLACE_TYPES = {"on-site", "onsite"}


class LeverSource:
    def __init__(self, site: str, http) -> None:
        self._site = site
        self._http = http

    def discover(self) -> list[Job]:
        try:
            data = self._http.get_json(_URL_TEMPLATE.format(site=self._site))
        except Exception as exc:
            if is_stale_board_error(exc):
                logger.info("lever board not found (404) for site %s", self._site)
            else:
                logger.warning(
                    "lever discovery failed for site %s", self._site, exc_info=True
                )
            return []

        jobs = []
        for item in data:
            categories = item.get("categories", {}) or {}
            workplace_type = (item.get("workplaceType") or "").lower()
            if workplace_type in _REMOTE_WORKPLACE_TYPES:
                remote = True
            elif workplace_type in _ONSITE_WORKPLACE_TYPES:
                remote = False
            else:
                remote = None

            description = item.get("descriptionPlain") or item.get("description", "")
            jobs.append(
                Job(
                    source="lever",
                    source_job_id=item.get("id"),
                    title=item.get("text", ""),
                    company=self._site,
                    location=categories.get("location", ""),
                    url=item.get("hostedUrl", ""),
                    description=strip_html(description),
                    remote=remote,
                )
            )
        return jobs


def fetch_description(site: str, target_url: str, http) -> str | None:
    data = http.get_json(_URL_TEMPLATE.format(site=site))
    target = canonicalize_url(target_url)
    for item in data:
        if canonicalize_url(item.get("hostedUrl", "")) == target:
            description = item.get("descriptionPlain") or item.get("description", "")
            return strip_html(description) or None
    return None

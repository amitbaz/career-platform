from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from job_hunter import content_confidence
from job_hunter.models import Job

if TYPE_CHECKING:
    from job_hunter.http import HttpClient


_SUPPORTED_ATS_HOSTS = {
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
}


def _find_job_posting(data: dict | list) -> dict | None:
    """Recursively find a JobPosting entry in a JSON-LD structure."""
    if isinstance(data, list):
        for item in data:
            result = _find_job_posting(item)
            if result is not None:
                return result
        return None
    if isinstance(data, dict):
        if data.get("@type") == "JobPosting":
            return data
        # Some schemas nest @graph
        for value in data.values():
            if isinstance(value, (dict, list)):
                result = _find_job_posting(value)
                if result is not None:
                    return result
    return None


def _iter_job_postings(data: object):
    """Yield every JSON-LD JobPosting object in a nested structure."""
    if isinstance(data, dict):
        if data.get("@type") == "JobPosting":
            yield data
        for value in data.values():
            yield from _iter_job_postings(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_job_postings(item)


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string, collapsing whitespace."""
    if not text:
        return text
    soup = BeautifulSoup(text, "html.parser")
    return " ".join(soup.get_text(separator=" ").split())


def extract_job_page_links(html: str, base_url: str) -> list[str]:
    """Extract absolute canonical, JobPosting, and supported ATS links from a page."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []

    for link in soup.find_all("link"):
        rel = {value.lower() for value in link.get("rel", [])}
        href = link.get("href")
        if "canonical" in rel and isinstance(href, str):
            links.append(urljoin(base_url, href))

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for posting in _iter_job_postings(data):
            url = posting.get("url")
            if isinstance(url, str):
                links.append(urljoin(base_url, url))

    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"])
        if urlparse(url).hostname in _SUPPORTED_ATS_HOSTS:
            links.append(url)

    return list(dict.fromkeys(links))


def extract_job_from_html(html: str) -> dict:
    """
    Parse job metadata from an HTML page.

    Searches all <script type="application/ld+json"> blocks for a JobPosting
    entry.  Falls back to page title and visible body text when no structured
    data is found.

    Returns a dict with (at minimum) whatever keys could be extracted:
    title, company, remote, description.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Try JSON-LD first ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        posting = _find_job_posting(data)
        if posting is None:
            continue

        result: dict = {}

        if title := posting.get("title"):
            result["title"] = title

        # Company name
        org = posting.get("hiringOrganization", {})
        if isinstance(org, dict):
            if name := org.get("name"):
                result["company"] = name
        elif isinstance(org, str):
            result["company"] = org

        # Remote flag
        job_location_type = posting.get("jobLocationType", "")
        result["remote"] = job_location_type == "TELECOMMUTE"

        # Description — strip HTML tags
        if desc := posting.get("description"):
            result["description"] = _strip_html(desc)
            result["description_confidence"] = content_confidence.CANONICAL_EMPLOYER_PAGE

        # Location
        location = posting.get("jobLocation", {})
        if isinstance(location, dict):
            address = location.get("address", {})
            if isinstance(address, dict):
                parts = [
                    address.get("addressLocality", ""),
                    address.get("addressRegion", ""),
                    address.get("addressCountry", ""),
                ]
                loc_str = ", ".join(p for p in parts if p)
                if loc_str:
                    result["location"] = loc_str
            elif isinstance(address, str):
                result["location"] = address

        # Salary
        if salary := posting.get("baseSalary"):
            result["salary"] = salary

        # Employment type
        if emp_type := posting.get("employmentType"):
            result["employment_type"] = emp_type

        return result

    # --- Fallback: page title + body text ---
    result = {}

    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        result["title"] = title_tag.string.strip()

    body_text = _strip_html(str(soup.body)) if soup.body else _strip_html(html)
    if body_text:
        result["description"] = body_text
        result["description_confidence"] = content_confidence.SOURCE_DETAIL_PAGE

    return result


def enrich_job(job: Job, http: HttpClient) -> Job:
    """
    Fetch job.url and fill in any missing fields from the page's JSON-LD.

    Only updates fields that are currently empty/None on the Job object.
    Returns the same (mutated) Job instance.
    """
    if not job.url:
        return job

    try:
        response = http.get(job.url)
        response.raise_for_status()
        data = extract_job_from_html(response.text)
    except Exception:
        return job

    if not job.title and (title := data.get("title")):
        job.title = title
    if not job.company and (company := data.get("company")):
        job.company = company
    if job.remote is None and "remote" in data:
        job.remote = data["remote"]
    if not job.description and (desc := data.get("description")):
        job.description = desc
        job.content_confidence = data.get(
            "description_confidence", content_confidence.SOURCE_DETAIL_PAGE
        )
    if not job.location and (loc := data.get("location")):
        job.location = loc

    return job

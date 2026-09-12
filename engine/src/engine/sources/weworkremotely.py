import xml.etree.ElementTree as ET
from collections.abc import Iterator

from engine.models import Job
from .base import strip_html

class WeWorkRemotelySource:

    # One board, one URL -- paginated from the same URL where it pages at
    # all -- so a 304 answers for the whole source (issue #184).
    crawl_is_one_resource = True
    source_label = "weworkremotely"

    def __init__(self, http, feed_urls=None):
        self._http = http
        self._feed_urls = feed_urls or [
            "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-product-jobs.rss",
        ]
    def discover(self) -> Iterator[Job]:
        """Yield each feed's jobs before fetching the feed after it."""
        for feed in self._feed_urls:
            try:
                response = self._http.get(feed)
                response.raise_for_status()
                root = ET.fromstring(response.text)
            except Exception:
                continue
            for item in root.findall(".//item"):
                raw_title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                if not raw_title or not link:
                    continue
                company, sep, title = raw_title.partition(":")
                yield Job(source="weworkremotely", title=title.strip() if sep else raw_title,
                          company=company.strip() if sep else "", url=link,
                          description=strip_html(item.findtext("description", "")), remote=True)

from collections.abc import Iterator

from .base import strip_html
from engine.models import Job

class RemoteOKSource:

    # One board, one URL -- paginated from the same URL where it pages at
    # all -- so a 304 answers for the whole source (issue #184).
    crawl_is_one_resource = True
    source_label = "remoteok"

    def __init__(self, http): self._http = http
    def discover(self) -> Iterator[Job]:
        data = self._http.get_json("https://remoteok.com/api")
        for x in data:
            if not x.get("position"):
                continue
            yield Job(source="remoteok", source_job_id=str(x["id"]) if x.get("id") is not None else None,
                      title=x.get("position", ""), company=x.get("company", ""), location=x.get("location", ""),
                      url=x.get("url", ""), description=strip_html(x.get("description", "")), remote=True)

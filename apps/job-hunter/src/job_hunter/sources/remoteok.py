from .base import strip_html
from job_hunter.models import Job

class RemoteOKSource:
    def __init__(self, http): self._http = http
    def discover(self) -> list[Job]:
        data = self._http.get_json("https://remoteok.com/api")
        return [Job(source="remoteok", source_job_id=str(x["id"]) if x.get("id") is not None else None,
                     title=x.get("position", ""), company=x.get("company", ""), location=x.get("location", ""),
                     url=x.get("url", ""), description=strip_html(x.get("description", "")), remote=True)
                for x in data if x.get("position")]

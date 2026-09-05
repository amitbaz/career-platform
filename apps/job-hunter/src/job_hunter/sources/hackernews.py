import re
from job_hunter.models import Job
from .base import strip_html

class HackerNewsHiringSource:
    def __init__(self, http): self._http = http
    def discover(self) -> list[Job]:
        search = self._http.get_json("https://hn.algolia.com/api/v1/search", params={"query": "Ask HN: Who is hiring?", "tags": "story"})
        hits = [h for h in search.get("hits", []) if (h.get("title") or "").startswith("Ask HN: Who is hiring?")]
        if not hits: return []
        hit = max(hits, key=lambda h: h.get("created_at_i", 0))
        item = self._http.get_json(f"https://hn.algolia.com/api/v1/items/{hit.get('objectID')}")
        jobs = []
        for child in item.get("children", []):
            text = strip_html(child.get("text", ""))
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 2 or not parts[0] or not parts[1]: continue
            match = re.search(r"https?://\S+", text)
            jobs.append(Job(source="hackernews", source_job_id=str(child.get("id")), company=parts[0], title=parts[1],
                            location=parts[2] if len(parts) > 2 else "", url=match.group(0).rstrip("),") if match else "",
                            description=text, remote="remote" in text.lower()))
        return jobs

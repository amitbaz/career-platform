from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests

_ARTIFACTS_URL_TEMPLATE = "https://api.github.com/repos/{repo}/actions/artifacts"
_DATABASE_NAME = "job_hunter.sqlite3"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def find_latest_artifact(
    repo: str,
    token: str,
    artifact_name: str,
    *,
    http=None,
) -> dict | None:
    http = http or requests
    url = (
        _ARTIFACTS_URL_TEMPLATE.format(repo=repo)
        + f"?name={quote(artifact_name)}&per_page=30"
    )
    response = http.get(url, headers=_headers(token))
    response.raise_for_status()
    artifacts = response.json().get("artifacts", [])
    candidates = [
        artifact
        for artifact in artifacts
        if artifact.get("name") == artifact_name and not artifact.get("expired")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda artifact: artifact["created_at"])


def _find_database_member(zf: zipfile.ZipFile, expected_name: str = _DATABASE_NAME) -> str:
    for info in zf.infolist():
        if info.is_dir():
            continue
        member_path = Path(info.filename)
        if member_path.name != expected_name:
            continue
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"Unsafe zip member path: {info.filename!r}")
        return info.filename
    raise ValueError(f"No member named {expected_name!r} found in artifact zip")


def extract_database(zip_content: bytes, dest: Path, expected_name: str = _DATABASE_NAME) -> Path:
    dest = Path(dest)
    with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
        member = _find_database_member(zf, expected_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, dest.open("wb") as out:
            out.write(src.read())
    return dest


def download_artifact_database(
    artifact: dict,
    token: str,
    dest: Path,
    *,
    http=None,
) -> Path:
    http = http or requests
    response = http.get(artifact["archive_download_url"], headers=_headers(token))
    response.raise_for_status()
    return extract_database(response.content, Path(dest))


def load_latest_artifact_to_path(
    repo: str,
    token: str,
    artifact_name: str,
    dest: Path,
    *,
    http=None,
) -> dict | None:
    artifact = find_latest_artifact(repo, token, artifact_name, http=http)
    if artifact is None:
        return None
    download_artifact_database(artifact, token, dest, http=http)
    return artifact


@dataclass(slots=True, frozen=True)
class ArtifactStateSnapshot:
    artifact_id: int
    path: Path
    created_at: str


class GitHubArtifactStateLoader:
    def __init__(
        self,
        repo: str,
        token: str,
        artifact_name: str = "job-hunter-state",
        cache_dir: Path | str = "/tmp/job-hunter-state",
        http=None,
    ) -> None:
        self._repo = repo
        self._token = token
        self._artifact_name = artifact_name
        self._cache_dir = Path(cache_dir)
        self._http = http or requests

    def load_latest(self) -> ArtifactStateSnapshot | None:
        artifact = find_latest_artifact(
            self._repo,
            self._token,
            self._artifact_name,
            http=self._http,
        )
        if artifact is None:
            return None

        artifact_id = int(artifact["id"])
        path = self._cache_dir / f"artifact-{artifact_id}" / _DATABASE_NAME
        if not path.exists():
            download_artifact_database(
                artifact,
                self._token,
                path,
                http=self._http,
            )

        return ArtifactStateSnapshot(
            artifact_id=artifact_id,
            path=path,
            created_at=artifact["created_at"],
        )

import io
import zipfile

import pytest

from job_hunter.github_state import GitHubArtifactStateLoader


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.content = content

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, artifacts, zip_data=b""):
        self.artifacts = artifacts
        self.zip_data = zip_data
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/zip"):
            return FakeResponse(200, content=self.zip_data)
        return FakeResponse(200, {"artifacts": self.artifacts})


def _artifact(id_, created_at, *, name="job-hunter-state", expired=False):
    return {
        "id": id_,
        "name": name,
        "expired": expired,
        "created_at": created_at,
        "archive_download_url": f"https://api.github.com/repos/acme/repo/actions/artifacts/{id_}/zip",
    }


def test_load_latest_downloads_newest_named_nonexpired_artifact(tmp_path):
    http = FakeHttp(
        [
            _artifact(1, "2026-08-30T00:00:00Z"),
            _artifact(2, "2026-08-31T00:00:00Z"),
            _artifact(3, "2026-09-01T00:00:00Z", expired=True),
            _artifact(4, "2026-09-01T00:00:00Z", name="other"),
        ],
        _zip_bytes({"nested/job_hunter.sqlite3": b"sqlite"}),
    )
    loader = GitHubArtifactStateLoader("acme/repo", "tok", "job-hunter-state", tmp_path, http=http)

    snapshot = loader.load_latest()

    assert snapshot is not None
    assert snapshot.artifact_id == 2
    assert snapshot.path.read_bytes() == b"sqlite"
    assert snapshot.created_at == "2026-08-31T00:00:00Z"
    assert http.calls[0][1]["headers"]["Authorization"] == "Bearer tok"
    assert http.calls[1][0].endswith("/2/zip")


def test_load_latest_reuses_cached_file_when_artifact_id_is_unchanged(tmp_path):
    http = FakeHttp(
        [_artifact(7, "2026-08-31T00:00:00Z")],
        _zip_bytes({"job_hunter.sqlite3": b"sqlite"}),
    )
    loader = GitHubArtifactStateLoader("acme/repo", "tok", "job-hunter-state", tmp_path, http=http)

    first = loader.load_latest()
    second = loader.load_latest()

    assert first is not None and second is not None
    assert first.path == second.path
    assert sum(1 for url, _ in http.calls if url.endswith("/zip")) == 1


def test_load_latest_rejects_unsafe_zip_member(tmp_path):
    http = FakeHttp(
        [_artifact(1, "2026-08-31T00:00:00Z")],
        _zip_bytes({"../job_hunter.sqlite3": b"bad"}),
    )
    loader = GitHubArtifactStateLoader("acme/repo", "tok", "job-hunter-state", tmp_path, http=http)

    with pytest.raises(ValueError):
        loader.load_latest()


def test_load_latest_returns_none_when_no_valid_artifact_exists(tmp_path):
    http = FakeHttp([], b"")
    loader = GitHubArtifactStateLoader("acme/repo", "tok", "job-hunter-state", tmp_path, http=http)
    assert loader.load_latest() is None

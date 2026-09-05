import io
import zipfile

import pytest

from scripts.restore_state import restore_state


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
    def __init__(self, list_response, download_response=None):
        self.list_response = list_response
        self.download_response = download_response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/zip"):
            return self.download_response
        return self.list_response


def _artifact(name="job-hunter-state", expired=False, created_at=None, id_=1):
    return {
        "id": id_,
        "name": name,
        "expired": expired,
        "created_at": created_at or "2026-08-30T07:05:00Z",
        "archive_download_url": f"https://api.github.com/repos/acme/repo/actions/artifacts/{id_}/zip",
    }


def test_restore_state_exits_ok_when_no_artifact_exists(tmp_path):
    http = FakeHttp(FakeResponse(200, {"artifacts": []}))
    dest = tmp_path / "job_hunter.sqlite3"

    result = restore_state(repo="acme/repo", token="tok", name="job-hunter-state", dest=dest, http=http)

    assert result is False
    assert not dest.exists()


def test_restore_state_ignores_expired_and_mismatched_artifacts(tmp_path):
    artifacts = [
        _artifact(name="other-artifact", id_=1),
        _artifact(expired=True, id_=2, created_at="2026-08-29T07:05:00Z"),
    ]
    http = FakeHttp(FakeResponse(200, {"artifacts": artifacts}))
    dest = tmp_path / "job_hunter.sqlite3"

    result = restore_state(repo="acme/repo", token="tok", name="job-hunter-state", dest=dest, http=http)

    assert result is False
    assert not dest.exists()


def test_restore_state_downloads_newest_matching_artifact(tmp_path):
    zip_data = _zip_bytes({"job_hunter.sqlite3": b"sqlite-bytes"})
    artifacts = [
        _artifact(id_=1, created_at="2026-08-29T07:05:00Z"),
        _artifact(id_=2, created_at="2026-08-30T07:05:00Z"),
    ]
    http = FakeHttp(
        FakeResponse(200, {"artifacts": artifacts}),
        FakeResponse(200, content=zip_data),
    )
    dest = tmp_path / "nested" / "job_hunter.sqlite3"

    result = restore_state(repo="acme/repo", token="tok", name="job-hunter-state", dest=dest, http=http)

    assert result is True
    assert dest.read_bytes() == b"sqlite-bytes"

    list_call = http.calls[0]
    assert list_call[1]["headers"]["Authorization"] == "Bearer tok"
    assert list_call[1]["headers"]["Accept"] == "application/vnd.github+json"

    download_call = http.calls[1]
    assert download_call[0] == "https://api.github.com/repos/acme/repo/actions/artifacts/2/zip"
    assert download_call[1]["headers"]["Authorization"] == "Bearer tok"


def test_restore_state_rejects_zip_slip_members(tmp_path):
    zip_data = _zip_bytes({"../../job_hunter.sqlite3": b"malicious"})
    artifacts = [_artifact(id_=1)]
    http = FakeHttp(
        FakeResponse(200, {"artifacts": artifacts}),
        FakeResponse(200, content=zip_data),
    )
    dest = tmp_path / "job_hunter.sqlite3"

    with pytest.raises(ValueError):
        restore_state(repo="acme/repo", token="tok", name="job-hunter-state", dest=dest, http=http)

    assert not dest.exists()
    assert not (tmp_path.parent.parent / "job_hunter.sqlite3").exists()


def test_restore_state_rejects_zip_with_no_matching_member(tmp_path):
    zip_data = _zip_bytes({"unrelated.txt": b"nope"})
    artifacts = [_artifact(id_=1)]
    http = FakeHttp(
        FakeResponse(200, {"artifacts": artifacts}),
        FakeResponse(200, content=zip_data),
    )
    dest = tmp_path / "job_hunter.sqlite3"

    with pytest.raises(ValueError):
        restore_state(repo="acme/repo", token="tok", name="job-hunter-state", dest=dest, http=http)

    assert not dest.exists()

# Job Hunter Per-User JWT and Supabase Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Job Hunter mint a short-lived JWT for one specific user and use it to reach the shared Supabase project, with row-level security proven to isolate that user against a live database.

**Architecture:** Four small units in `apps/job-hunter`: a settings loader that reads the run's user id and a private signing key from the environment, a token minter that signs an ES256 JWT carrying `sub` and `role: authenticated`, a minimal PostgREST client that attaches that token plus the publishable key to every request, and two new verbs on the existing shared HTTP helper. Nothing is wired into the pipeline — issue #70 does that.

**Tech Stack:** Python 3.12, pytest, `requests` (existing), `PyJWT[crypto]` (new), Supabase CLI local stack, PostgREST.

**Spec:** `docs/superpowers/specs/2026-09-06-job-hunter-per-user-jwt-design.md`

## Global Constraints

- Work on branch `feat/job-hunter-per-user-jwt`. Never commit to `main`.
- Commits follow loose Conventional Commits; `job-hunter:` is an acceptable prefix when a change is entirely inside that app (`CONTRIBUTING.md`).
- The service-role key and the legacy shared JWT secret must not appear anywhere in application code, tests, CI, or documentation.
- The signing key, the decoded JWK, and any minted token must never be written to logs, to disk inside the repository, or to an error message.
- Do not modify `pipeline.py`, `cli.py`, or `store.py`. Job Hunter's runtime keeps using SQLite; wiring Postgres in is issue #70.
- Do not add a `supabase` / `postgrest` / `httpx` dependency. The only new runtime dependency is `PyJWT[crypto]`.
- Run tests through the app's own virtualenv: `apps/job-hunter/.venv/bin/pytest`. A stale global install at `~/job-hunter-bot` hijacks a bare `python -m pytest`.
- Install with `pip install -e '.[test,webhook]'` — the full suite imports flask.
- Token claims are exactly `sub`, `role`, `exp`. Header carries `alg: ES256`, `kid`, `typ: JWT`.
- Token lifetime is 300 seconds; re-mint when fewer than 60 seconds remain.
- Test users are the same two UUIDs the pgTAP suite already uses: A is `aaaaaaaa-0000-0000-0000-000000000001`, B is `bbbbbbbb-0000-0000-0000-000000000002`.

---

### Task 1: Confirm the local stack accepts an ES256 token

This is a gate, not a code change. The spec flags it as the one unverified assumption the whole design rests on: the Supabase config template exposes `signing_keys_path` and the CLI's token generator reads it, but nothing confirms the local PostgREST accepts a token signed by that key. Find out before writing code against it.

**Files:**
- Modify: `supabase/config.toml:168` (uncomment `signing_keys_path`)
- Create: `supabase/signing_keys.json` (generated, git-ignored — added to `.gitignore` in Task 6)

- [ ] **Step 1: Generate a local-only signing key**

From the repository root:

```bash
supabase gen signing-key --algorithm ES256 > /tmp/jwk.json
python3 -c "import json,sys; json.dump([json.load(open('/tmp/jwk.json'))], open('supabase/signing_keys.json','w'))"
```

The generator prints a single JWK object. The keys file is a JSON array of JWKs, hence the wrapping. If a later step reports that the file cannot be parsed, retry with the bare object instead of an array and record which shape worked.

- [ ] **Step 2: Point the local stack at it**

In `supabase/config.toml`, uncomment line 168 so it reads:

```toml
signing_keys_path = "./signing_keys.json"
```

- [ ] **Step 3: Restart the stack so the new key is loaded**

```bash
supabase stop && supabase start
```

- [ ] **Step 4: Mint a token and call PostgREST with it**

```bash
eval "$(supabase status -o env | sed 's/^/export /')"
TOKEN=$(supabase gen bearer-jwt --role authenticated --sub aaaaaaaa-0000-0000-0000-000000000001 --valid-for 5m)
curl -s -w '\nHTTP %{http_code}\n' \
  -H "apikey: $ANON_KEY" \
  -H "Authorization: Bearer $TOKEN" \
  "$API_URL/rest/v1/job_hunter_jobs?select=id&limit=1"
```

Expected: `[]` and `HTTP 200`. An empty list is the correct answer — that user owns no rows, and row-level security filters rather than erroring.

- [ ] **Step 5: Decide whether the plan continues**

`HTTP 200` means the local stack honours the imported ES256 key and every later task is valid as written.

`HTTP 401` with a message about an invalid JWT means it does not. **Stop and report to the user.** Do not fall back silently. The spec names the fallback — run local tests against the legacy HS256 secret while production uses ES256 — and calls it a reason to revisit the signing-key decision, not something to paper over.

- [ ] **Step 6: Do not commit**

Nothing here is committed. `supabase/signing_keys.json` is generated per machine and gets its `.gitignore` entry in Task 6; the `config.toml` edit is committed there too, alongside the rest of the local test setup it belongs to.

---

### Task 2: Supabase settings loader

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/config.py` (add `SupabaseSettings` near `WebhookSettings` at line 31; add `load_supabase_settings()` near `load_webhook_settings()` at line 135)
- Test: `apps/job-hunter/tests/test_supabase_settings.py`

**Interfaces:**
- Consumes: `_require_env(name: str) -> str`, the existing loader helper in the same module.
- Produces: `SupabaseSettings(user_id: str, url: str, publishable_key: str, signing_key_jwk: dict)` and `load_supabase_settings() -> SupabaseSettings`. Task 4 consumes `user_id` and `signing_key_jwk`; Task 5 consumes `url` and `publishable_key`.

- [ ] **Step 1: Write the failing tests**

Create `apps/job-hunter/tests/test_supabase_settings.py`:

```python
import base64
import json

import pytest

from job_hunter.config import load_supabase_settings

VALID_JWK = {
    "kty": "EC",
    "kid": "11111111-2222-3333-4444-555555555555",
    "alg": "ES256",
    "crv": "P-256",
    "d": "ROsbtI7IzXA9aF9O60sCUheqjrmenjRbZYYirWO9Kn8",
    "x": "S-EfNzQOiAhLH7jdkWWUXeMtt2GEqDI-GdTuK7RWNUA",
    "y": "IgKTms8k072_kvlmjOsDIdIUrA9WYtLuHlLyd_6OxHc",
}


def _encode(jwk: dict) -> str:
    return base64.b64encode(json.dumps(jwk).encode("utf-8")).decode("ascii")


def _set_all(monkeypatch, **overrides) -> None:
    values = {
        "JOB_HUNTER_USER_ID": "aaaaaaaa-0000-0000-0000-000000000001",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_example",
        "SUPABASE_SIGNING_KEY_B64": _encode(VALID_JWK),
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_loads_all_settings(monkeypatch):
    _set_all(monkeypatch)

    settings = load_supabase_settings()

    assert settings.user_id == "aaaaaaaa-0000-0000-0000-000000000001"
    assert settings.url == "https://example.supabase.co"
    assert settings.publishable_key == "sb_publishable_example"
    assert settings.signing_key_jwk == VALID_JWK


def test_strips_trailing_slash_from_url(monkeypatch):
    _set_all(monkeypatch, SUPABASE_URL="https://example.supabase.co/")

    assert load_supabase_settings().url == "https://example.supabase.co"


def test_missing_variable_raises(monkeypatch):
    _set_all(monkeypatch, SUPABASE_URL=None)

    with pytest.raises(ValueError, match="SUPABASE_URL"):
        load_supabase_settings()


def test_non_uuid_user_id_raises(monkeypatch):
    _set_all(monkeypatch, JOB_HUNTER_USER_ID="not-a-uuid")

    with pytest.raises(ValueError, match="JOB_HUNTER_USER_ID must be a UUID"):
        load_supabase_settings()


def test_malformed_signing_key_raises_without_echoing_it(monkeypatch):
    secret = base64.b64encode(b"not json at all").decode("ascii")
    _set_all(monkeypatch, SUPABASE_SIGNING_KEY_B64=secret)

    with pytest.raises(ValueError) as excinfo:
        load_supabase_settings()

    assert "SUPABASE_SIGNING_KEY_B64" in str(excinfo.value)
    assert secret not in str(excinfo.value)
    assert "not json" not in str(excinfo.value)


def test_signing_key_without_kid_raises(monkeypatch):
    incomplete = {key: value for key, value in VALID_JWK.items() if key != "kid"}
    _set_all(monkeypatch, SUPABASE_SIGNING_KEY_B64=_encode(incomplete))

    with pytest.raises(ValueError, match="kid"):
        load_supabase_settings()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `apps/job-hunter/.venv/bin/pytest tests/test_supabase_settings.py -q` from `apps/job-hunter`
Expected: FAIL — `ImportError: cannot import name 'load_supabase_settings'`

- [ ] **Step 3: Add the dataclass**

In `config.py`, immediately after the `WebhookSettings` dataclass (ends line 38):

```python
@dataclass(slots=True, frozen=True)
class SupabaseSettings:
    """Credentials for acting as one user against the shared Supabase project.

    ``signing_key_jwk`` is the private half of the project's ES256 signing key.
    It is held in memory only and must never be logged or written to disk.
    """

    user_id: str
    url: str
    publishable_key: str
    signing_key_jwk: dict
```

- [ ] **Step 4: Add the loader**

In `config.py`, immediately after `load_webhook_settings()`:

```python
def load_supabase_settings() -> SupabaseSettings:
    raw_user_id = _require_env("JOB_HUNTER_USER_ID")
    try:
        uuid.UUID(raw_user_id)
    except ValueError as exc:
        raise ValueError("JOB_HUNTER_USER_ID must be a UUID") from exc

    return SupabaseSettings(
        user_id=raw_user_id,
        url=_require_env("SUPABASE_URL").rstrip("/"),
        publishable_key=_require_env("SUPABASE_PUBLISHABLE_KEY"),
        signing_key_jwk=_decode_signing_key(_require_env("SUPABASE_SIGNING_KEY_B64")),
    )


def _decode_signing_key(encoded: str) -> dict:
    """Decode the base64-encoded private JWK.

    Error messages deliberately omit the offending value: it is key material.
    """
    try:
        jwk = json.loads(base64.b64decode(encoded))
    except Exception:  # never surface the key material in the message
        raise ValueError(
            "SUPABASE_SIGNING_KEY_B64 must be base64-encoded JSON"
        ) from None
    if not isinstance(jwk, dict) or not jwk.get("kid") or not jwk.get("kty"):
        raise ValueError(
            "SUPABASE_SIGNING_KEY_B64 must decode to a JWK object with 'kid' and 'kty'"
        )
    return jwk
```

Add `import json` and `import uuid` to the module's imports (it already imports `base64` and `os`).

Note the `from None` on the decode failure: chaining would attach the original exception, and a JSON decode error quotes the text it choked on — which is the key.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `apps/job-hunter/.venv/bin/pytest tests/test_supabase_settings.py -q`
Expected: 6 passed

- [ ] **Step 6: Run the full suite**

Run: `apps/job-hunter/.venv/bin/pytest -q`
Expected: all pass, no new failures

- [ ] **Step 7: Commit**

```bash
git add apps/job-hunter/src/job_hunter/config.py apps/job-hunter/tests/test_supabase_settings.py
git commit -m "job-hunter: load per-user Supabase settings from the environment"
```

---

### Task 3: PATCH and DELETE on the shared HTTP helper

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/http.py:53` (add two methods after `post`)
- Test: `apps/job-hunter/tests/test_http.py` (append)

**Interfaces:**
- Produces: `HttpClient.patch(url, *, retry_status_codes=None, retry=True, **kwargs) -> requests.Response` and `HttpClient.delete(...)` with the identical signature. Task 5 calls both.

- [ ] **Step 1: Write the failing tests**

Append to `apps/job-hunter/tests/test_http.py` (it already defines `FakeResponse` and a session-patching helper — reuse them; read the top of the file before writing):

```python
def test_patch_sends_patch_and_returns_response(monkeypatch):
    client = HttpClient()
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        return FakeResponse(200)

    monkeypatch.setattr(client._session, "request", fake_request)

    response = client.patch("https://example.test/rows", json={"a": 1})

    assert seen["method"] == "PATCH"
    assert seen["url"] == "https://example.test/rows"
    assert response.status_code == 200


def test_delete_sends_delete_and_returns_response(monkeypatch):
    client = HttpClient()
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["method"] = method
        return FakeResponse(200)

    monkeypatch.setattr(client._session, "request", fake_request)

    assert client.delete("https://example.test/rows").status_code == 200
    assert seen["method"] == "DELETE"


def test_patch_applies_the_default_timeout(monkeypatch):
    client = HttpClient()
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return FakeResponse(200)

    monkeypatch.setattr(client._session, "request", fake_request)
    client.patch("https://example.test/rows")

    assert seen["timeout"] == (5, 25)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `apps/job-hunter/.venv/bin/pytest tests/test_http.py -q`
Expected: FAIL — `AttributeError: 'HttpClient' object has no attribute 'patch'`

- [ ] **Step 3: Add the two methods**

In `http.py`, after `post` (line 53):

```python
    def patch(
        self,
        url: str,
        *,
        retry_status_codes: set[int] | None = None,
        retry: bool = True,
        **kwargs,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._request(
            "PATCH",
            url,
            retry_status_codes=retry_status_codes,
            retry=retry,
            **kwargs,
        )

    def delete(
        self,
        url: str,
        *,
        retry_status_codes: set[int] | None = None,
        retry: bool = True,
        **kwargs,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._request(
            "DELETE",
            url,
            retry_status_codes=retry_status_codes,
            retry=retry,
            **kwargs,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `apps/job-hunter/.venv/bin/pytest tests/test_http.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/http.py apps/job-hunter/tests/test_http.py
git commit -m "job-hunter: add PATCH and DELETE to the shared HTTP client"
```

---

### Task 4: Access token minter

**Files:**
- Create: `apps/job-hunter/src/job_hunter/supabase_auth.py`
- Modify: `apps/job-hunter/pyproject.toml:9-16` (add the dependency)
- Test: `apps/job-hunter/tests/test_supabase_auth.py`

**Interfaces:**
- Consumes: `SupabaseSettings.user_id` and `SupabaseSettings.signing_key_jwk` from Task 2.
- Produces: `AccessTokenMinter(user_id: str, signing_key_jwk: dict)` with one public method `token() -> str`. Task 5 calls `token()` on every request.

- [ ] **Step 1: Add the dependency and reinstall**

In `apps/job-hunter/pyproject.toml`, add to `dependencies`:

```toml
    "PyJWT[crypto]>=2.8",
```

Then, from `apps/job-hunter`:

```bash
.venv/bin/pip install -e '.[test,webhook]'
```

- [ ] **Step 2: Write the failing tests**

Create `apps/job-hunter/tests/test_supabase_auth.py`:

```python
import json
import logging

import jwt
import pytest

from job_hunter.supabase_auth import AccessTokenMinter

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"

PRIVATE_JWK = {
    "kty": "EC",
    "kid": "11111111-2222-3333-4444-555555555555",
    "alg": "ES256",
    "crv": "P-256",
    "d": "ROsbtI7IzXA9aF9O60sCUheqjrmenjRbZYYirWO9Kn8",
    "x": "S-EfNzQOiAhLH7jdkWWUXeMtt2GEqDI-GdTuK7RWNUA",
    "y": "IgKTms8k072_kvlmjOsDIdIUrA9WYtLuHlLyd_6OxHc",
}


def _public_key():
    public = {k: v for k, v in PRIVATE_JWK.items() if k != "d"}
    return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(public))


def test_token_carries_the_expected_claims():
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    token = minter.token()
    claims = jwt.decode(token, _public_key(), algorithms=["ES256"])

    assert claims["sub"] == USER_A
    assert claims["role"] == "authenticated"
    assert set(claims) == {"sub", "role", "exp"}


def test_token_header_names_the_key():
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    header = jwt.get_unverified_header(minter.token())

    assert header["alg"] == "ES256"
    assert header["kid"] == PRIVATE_JWK["kid"]
    assert header["typ"] == "JWT"


def test_token_expires_five_minutes_out(monkeypatch):
    monkeypatch.setattr("job_hunter.supabase_auth.time.time", lambda: 1_000_000.0)
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    claims = jwt.decode(
        minter.token(), _public_key(), algorithms=["ES256"], options={"verify_exp": False}
    )

    assert claims["exp"] == 1_000_300


def test_a_fresh_token_is_reused(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("job_hunter.supabase_auth.time.time", lambda: clock["now"])
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    first = minter.token()
    clock["now"] += 100
    assert minter.token() == first


def test_an_expiring_token_is_reminted(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("job_hunter.supabase_auth.time.time", lambda: clock["now"])
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    first = minter.token()
    clock["now"] += 250  # 50 seconds left, inside the 60 second margin
    second = minter.token()

    assert second != first
    claims = jwt.decode(
        second, _public_key(), algorithms=["ES256"], options={"verify_exp": False}
    )
    assert claims["exp"] == 1_000_550


def test_nothing_secret_reaches_the_logs(caplog):
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    with caplog.at_level(logging.DEBUG):
        token = minter.token()

    assert token not in caplog.text
    assert PRIVATE_JWK["d"] not in caplog.text


def test_a_key_without_a_private_half_is_rejected():
    public_only = {k: v for k, v in PRIVATE_JWK.items() if k != "d"}

    with pytest.raises(ValueError, match="private"):
        AccessTokenMinter(USER_A, public_only)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `apps/job-hunter/.venv/bin/pytest tests/test_supabase_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'job_hunter.supabase_auth'`

- [ ] **Step 4: Write the minter**

Create `apps/job-hunter/src/job_hunter/supabase_auth.py`:

```python
"""Minting short-lived access tokens for one Supabase user.

Job Hunter is a batch process: there is no end-user session to inherit, so it
signs its own token naming the user a run acts for. Postgres row-level security
reads the ``sub`` claim through ``auth.uid()`` and only returns that user's rows,
so the token is the whole basis of data isolation — see
docs/superpowers/specs/2026-09-06-job-hunter-per-user-jwt-design.md.

Nothing here is ever logged: both the private key and the minted token grant
access to the user's data.
"""

from __future__ import annotations

import json
import time

import jwt
from jwt.algorithms import ECAlgorithm

_LIFETIME_SECONDS = 300
_REFRESH_MARGIN_SECONDS = 60


class AccessTokenMinter:
    """Issues ES256 access tokens for a single user.

    The token is cached and reused until it is within
    ``_REFRESH_MARGIN_SECONDS`` of expiry, so a long run re-mints a handful of
    times rather than holding one token valid for its whole duration.
    """

    def __init__(self, user_id: str, signing_key_jwk: dict) -> None:
        if not signing_key_jwk.get("d"):
            raise ValueError("signing key JWK has no private component ('d')")
        self._user_id = user_id
        self._kid = signing_key_jwk["kid"]
        self._key = ECAlgorithm.from_jwk(json.dumps(signing_key_jwk))
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        """Return a currently-valid access token, minting one if needed."""
        now = time.time()
        if self._token is None or self._expires_at - now <= _REFRESH_MARGIN_SECONDS:
            self._mint(now)
        assert self._token is not None
        return self._token

    def _mint(self, now: float) -> None:
        expires_at = int(now) + _LIFETIME_SECONDS
        self._token = jwt.encode(
            {
                "sub": self._user_id,
                "role": "authenticated",
                "exp": expires_at,
            },
            self._key,
            algorithm="ES256",
            headers={"kid": self._kid},
        )
        self._expires_at = float(expires_at)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `apps/job-hunter/.venv/bin/pytest tests/test_supabase_auth.py -q`
Expected: 7 passed

- [ ] **Step 6: Run the full suite**

Run: `apps/job-hunter/.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add apps/job-hunter/pyproject.toml apps/job-hunter/src/job_hunter/supabase_auth.py apps/job-hunter/tests/test_supabase_auth.py
git commit -m "job-hunter: mint short-lived per-user Supabase access tokens"
```

---

### Task 5: Minimal PostgREST client

**Files:**
- Create: `apps/job-hunter/src/job_hunter/supabase_client.py`
- Test: `apps/job-hunter/tests/test_supabase_client.py`

**Interfaces:**
- Consumes: `HttpClient.get/post/patch/delete` (Tasks 3), `SupabaseSettings` (Task 2), `AccessTokenMinter.token()` (Task 4).
- Produces:
  - `SupabaseError(RuntimeError)`, `SupabaseAuthError(SupabaseError)` (HTTP 401), `SupabasePermissionError(SupabaseError)` (HTTP 403), `SupabaseRequestError(SupabaseError)` (any other failure status).
  - `SupabaseClient(http: HttpClient, settings: SupabaseSettings, minter: AccessTokenMinter)` with `select(table, *, params=None) -> list[dict]`, `insert(table, rows: list[dict]) -> list[dict]`, `update(table, values: dict, *, params: dict) -> list[dict]`, `delete(table, *, params: dict) -> list[dict]`.
  - Task 7 uses all four.

- [ ] **Step 1: Write the failing tests**

Create `apps/job-hunter/tests/test_supabase_client.py`:

```python
import json

import pytest

from job_hunter.config import SupabaseSettings
from job_hunter.supabase_client import (
    SupabaseAuthError,
    SupabaseClient,
    SupabasePermissionError,
    SupabaseRequestError,
)

SETTINGS = SupabaseSettings(
    user_id="aaaaaaaa-0000-0000-0000-000000000001",
    url="https://example.supabase.co",
    publishable_key="sb_publishable_example",
    signing_key_jwk={"kid": "k", "kty": "EC"},
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = [] if payload is None else payload
        self.text = text

    def json(self):
        return self._payload


class FakeMinter:
    def token(self) -> str:
        return "test-token"


class FakeHttp:
    """Records every call and returns queued responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses) or [FakeResponse(200)]
        self.calls = []

    def _record(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0) if self.responses else FakeResponse(200)

    def get(self, url, **kwargs):
        return self._record("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._record("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._record("DELETE", url, **kwargs)


def _client(*responses):
    http = FakeHttp(*responses)
    return SupabaseClient(http, SETTINGS, FakeMinter()), http


def test_select_builds_the_url_and_passes_filters():
    client, http = _client(FakeResponse(200, [{"id": "1"}]))

    rows = client.select("job_hunter_jobs", params={"select": "id", "status": "eq.new"})

    assert rows == [{"id": "1"}]
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://example.supabase.co/rest/v1/job_hunter_jobs"
    assert call["params"] == {"select": "id", "status": "eq.new"}


def test_every_request_carries_both_auth_headers():
    client, http = _client()

    client.select("job_hunter_jobs")

    headers = http.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["apikey"] == "sb_publishable_example"


def test_insert_posts_rows_and_asks_for_them_back():
    client, http = _client(FakeResponse(201, [{"id": "1"}]))

    rows = client.insert("job_hunter_jobs", [{"fingerprint": "f"}])

    assert rows == [{"id": "1"}]
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == [{"fingerprint": "f"}]
    assert call["headers"]["Prefer"] == "return=representation"


def test_update_patches_with_filters():
    client, http = _client(FakeResponse(200, [{"id": "1"}]))

    rows = client.update("job_hunter_jobs", {"status": "seen"}, params={"id": "eq.1"})

    assert rows == [{"id": "1"}]
    call = http.calls[0]
    assert call["method"] == "PATCH"
    assert call["json"] == {"status": "seen"}
    assert call["params"] == {"id": "eq.1"}


def test_delete_sends_filters():
    client, http = _client(FakeResponse(200, []))

    assert client.delete("job_hunter_jobs", params={"id": "eq.1"}) == []
    assert http.calls[0]["method"] == "DELETE"


@pytest.mark.parametrize("method", ["update", "delete"])
def test_unfiltered_writes_are_refused(method):
    client, _ = _client()

    with pytest.raises(ValueError, match="filter"):
        if method == "update":
            client.update("job_hunter_jobs", {"status": "seen"}, params={})
        else:
            client.delete("job_hunter_jobs", params={})


def test_401_raises_an_auth_error():
    client, _ = _client(FakeResponse(401, text="JWT expired"))

    with pytest.raises(SupabaseAuthError):
        client.select("job_hunter_jobs")


def test_403_raises_a_permission_error():
    client, _ = _client(FakeResponse(403, text="row-level security"))

    with pytest.raises(SupabasePermissionError):
        client.insert("job_hunter_jobs", [{"fingerprint": "f"}])


def test_other_failures_raise_a_request_error_naming_the_status():
    client, _ = _client(FakeResponse(409, text="duplicate key"))

    with pytest.raises(SupabaseRequestError, match="409"):
        client.insert("job_hunter_jobs", [{"fingerprint": "f"}])


def test_errors_do_not_leak_the_token():
    client, _ = _client(FakeResponse(500, text="boom"))

    with pytest.raises(SupabaseRequestError) as excinfo:
        client.select("job_hunter_jobs")

    assert "test-token" not in str(excinfo.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `apps/job-hunter/.venv/bin/pytest tests/test_supabase_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'job_hunter.supabase_client'`

- [ ] **Step 3: Write the client**

Create `apps/job-hunter/src/job_hunter/supabase_client.py`:

```python
"""A minimal PostgREST client that acts as one Supabase user.

Deliberately narrow: it covers what issue #69 needs to prove data isolation,
and no more. Issue #70 grows it with the query surface the ported store
actually needs.

Two headers go on every request. ``Authorization`` carries the short-lived
token that decides which rows row-level security will return; ``apikey``
carries the project's publishable key, which Supabase requires separately and
which is public by design. A minted token is not valid in the ``apikey``
header.

Caveat for callers: the shared HttpClient retries 5xx responses, including on
POST. Every job_hunter_* table has a user-scoped unique key, so a retried
insert conflicts rather than duplicating, but a caller relying on
non-idempotent writes should pass ``retry=False`` itself.
"""

from __future__ import annotations

from typing import Any

from .config import SupabaseSettings
from .http import HttpClient
from .supabase_auth import AccessTokenMinter


class SupabaseError(RuntimeError):
    """Base class for every failed Supabase request."""


class SupabaseAuthError(SupabaseError):
    """HTTP 401 — the token was missing, malformed, or expired."""


class SupabasePermissionError(SupabaseError):
    """HTTP 403 — a row-level security policy refused the write."""


class SupabaseRequestError(SupabaseError):
    """Any other failure status."""


class SupabaseClient:
    def __init__(
        self,
        http: HttpClient,
        settings: SupabaseSettings,
        minter: AccessTokenMinter,
    ) -> None:
        self._http = http
        self._settings = settings
        self._minter = minter

    def select(
        self, table: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        response = self._http.get(
            self._url(table), headers=self._headers(), params=params or {}
        )
        return self._parse(response)

    def insert(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        response = self._http.post(
            self._url(table), headers=self._headers(write=True), json=rows
        )
        return self._parse(response)

    def update(
        self, table: str, values: dict[str, Any], *, params: dict[str, str]
    ) -> list[dict[str, Any]]:
        self._require_filter(params)
        response = self._http.patch(
            self._url(table),
            headers=self._headers(write=True),
            params=params,
            json=values,
        )
        return self._parse(response)

    def delete(self, table: str, *, params: dict[str, str]) -> list[dict[str, Any]]:
        self._require_filter(params)
        response = self._http.delete(
            self._url(table), headers=self._headers(write=True), params=params
        )
        return self._parse(response)

    def _url(self, table: str) -> str:
        return f"{self._settings.url}/rest/v1/{table}"

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._minter.token()}",
            "apikey": self._settings.publishable_key,
            "Accept": "application/json",
        }
        if write:
            headers["Content-Type"] = "application/json"
            headers["Prefer"] = "return=representation"
        return headers

    @staticmethod
    def _require_filter(params: dict[str, str]) -> None:
        """Refuse an unfiltered update or delete.

        PostgREST applies a filterless write to every row the caller can see.
        Row-level security limits that to the caller's own rows, which is still
        their entire dataset.
        """
        if not params:
            raise ValueError("update and delete require a filter in params")

    @staticmethod
    def _parse(response) -> list[dict[str, Any]]:
        if response.status_code == 401:
            raise SupabaseAuthError("Supabase rejected the access token (401)")
        if response.status_code == 403:
            raise SupabasePermissionError(
                "a row-level security policy refused the request (403)"
            )
        if response.status_code >= 400:
            raise SupabaseRequestError(
                f"Supabase request failed with {response.status_code}: {response.text}"
            )
        if response.status_code == 204 or not response.text:
            return []
        payload = response.json()
        return payload if isinstance(payload, list) else [payload]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `apps/job-hunter/.venv/bin/pytest tests/test_supabase_client.py -q`
Expected: 11 passed

- [ ] **Step 5: Run the full suite**

Run: `apps/job-hunter/.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add apps/job-hunter/src/job_hunter/supabase_client.py apps/job-hunter/tests/test_supabase_client.py
git commit -m "job-hunter: add a minimal per-user PostgREST client"
```

---

### Task 6: Local test fixtures for a live stack

**Files:**
- Create: `supabase/seed.sql`
- Modify: `supabase/.gitignore` (ignore the generated signing key)
- Modify: `supabase/config.toml:168` (enable `signing_keys_path` — the edit made in Task 1)
- Modify: `apps/job-hunter/.env.example` (document the four new variables)

**Interfaces:**
- Produces: two seeded users in the local stack, A `aaaaaaaa-0000-0000-0000-000000000001` and B `bbbbbbbb-0000-0000-0000-000000000002`, matching the UUIDs the pgTAP suite already uses. Task 7 signs tokens for both.

- [ ] **Step 1: Write the seed file**

Create `supabase/seed.sql`:

```sql
-- Local development and test fixtures. Applied by `supabase db reset` and by
-- the first `supabase start`; never runs against the hosted project.
--
-- Two users, A and B, with the same fixed UUIDs the pgTAP isolation suite
-- uses (supabase/tests/pgtap/job_hunter_isolation.sql). Job Hunter's Python
-- integration test signs a token for each and proves that a run for A cannot
-- reach B's rows through the live policies.

insert into auth.users (
  id, email, instance_id, aud, role,
  raw_app_meta_data, raw_user_meta_data, created_at, updated_at
)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'a@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'b@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now())
on conflict (id) do nothing;
```

- [ ] **Step 2: Ignore the generated signing key**

Append to `supabase/.gitignore`:

```
# Generated per machine by `supabase gen signing-key`; never commit key material
signing_keys.json
```

- [ ] **Step 3: Document the new environment variables**

Append to `apps/job-hunter/.env.example`:

```bash
# Supabase (issue #69). Not yet used by the pipeline — the store port is #70.
# JOB_HUNTER_USER_ID is the UUID of the platform user a run acts for.
# SUPABASE_SIGNING_KEY_B64 is the base64-encoded private JWK of the project's
# ES256 signing key. It mints tokens for any user: treat it as the platform's
# most sensitive secret.
JOB_HUNTER_USER_ID=
SUPABASE_URL=
SUPABASE_PUBLISHABLE_KEY=
SUPABASE_SIGNING_KEY_B64=
```

- [ ] **Step 4: Apply the seed and confirm both users exist**

```bash
supabase db reset
supabase status -o env | grep -E 'DB_URL|API_URL|ANON_KEY'
psql "$(supabase status -o env | sed -n 's/^DB_URL=//p' | tr -d '"')" \
  -c "select id, email from auth.users order by email;"
```

Expected: two rows, `a@test.local` and `b@test.local`.

- [ ] **Step 5: Confirm the key file is not tracked**

```bash
git status --short supabase/
```

Expected: `supabase/seed.sql` and the modified `.gitignore` / `config.toml` appear; `supabase/signing_keys.json` does not.

- [ ] **Step 6: Commit**

```bash
git add supabase/seed.sql supabase/.gitignore supabase/config.toml apps/job-hunter/.env.example
git commit -m "test: seed two local users and enable local JWT signing keys"
```

---

### Task 7: Live isolation test

This is the ticket's acceptance criterion: a run for one user cannot read or write another user's rows, verified against the live policies.

**Files:**
- Create: `apps/job-hunter/tests/integration/__init__.py` (empty)
- Create: `apps/job-hunter/tests/integration/test_supabase_isolation.py`
- Modify: `apps/job-hunter/pyproject.toml:27-28` (register the `integration` marker)
- Modify: `apps/job-hunter/AGENTS.md:24` (the rule this test sits against)

**Interfaces:**
- Consumes: everything from Tasks 2–5, plus the seeded users from Task 6.

- [ ] **Step 1: Register the marker**

In `apps/job-hunter/pyproject.toml`, extend the pytest section:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "integration: needs a running Supabase stack; skipped when none is reachable",
]
```

- [ ] **Step 2: Write the failing test**

Create `apps/job-hunter/tests/integration/__init__.py` as an empty file, then `apps/job-hunter/tests/integration/test_supabase_isolation.py`:

```python
"""Proves per-user isolation against live row-level security policies.

Skipped unless a Supabase stack is reachable and its details are exported.
Get them with:

    eval "$(supabase status -o env | sed 's/^/export /')"
    export SUPABASE_TEST_URL="$API_URL"
    export SUPABASE_TEST_PUBLISHABLE_KEY="$ANON_KEY"
    export SUPABASE_TEST_SIGNING_KEY_B64="$(base64 < supabase/signing_keys_single.json)"

where signing_keys_single.json holds the single JWK object (the stack's
signing_keys.json wraps it in an array).
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timezone

import pytest

from job_hunter.config import SupabaseSettings
from job_hunter.http import HttpClient
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient, SupabasePermissionError

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"
TABLE = "job_hunter_jobs"

_URL = os.environ.get("SUPABASE_TEST_URL")
_KEY = os.environ.get("SUPABASE_TEST_PUBLISHABLE_KEY")
_JWK = os.environ.get("SUPABASE_TEST_SIGNING_KEY_B64")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_URL and _KEY and _JWK),
        reason="no Supabase stack configured; see this module's docstring",
    ),
]


def _client_for(user_id: str) -> SupabaseClient:
    jwk = json.loads(base64.b64decode(_JWK))
    settings = SupabaseSettings(
        user_id=user_id,
        url=_URL.rstrip("/"),
        publishable_key=_KEY,
        signing_key_jwk=jwk,
    )
    return SupabaseClient(HttpClient(), settings, AccessTokenMinter(user_id, jwk))


@pytest.fixture
def as_a() -> SupabaseClient:
    return _client_for(USER_A)


@pytest.fixture
def as_b() -> SupabaseClient:
    return _client_for(USER_B)


@pytest.fixture
def a_row(as_a: SupabaseClient):
    """A row owned by user A, removed when the test finishes."""
    now = datetime.now(timezone.utc).isoformat()
    fingerprint = f"isolation-test-{uuid.uuid4()}"
    rows = as_a.insert(
        TABLE,
        [
            {
                "user_id": USER_A,
                "fingerprint": fingerprint,
                "title": "Original title",
                "first_seen_at": now,
                "last_seen_at": now,
            }
        ],
    )
    assert len(rows) == 1
    yield rows[0]
    as_a.delete(TABLE, params={"id": f"eq.{rows[0]['id']}"})


def test_a_reads_back_its_own_row(as_a, a_row):
    found = as_a.select(TABLE, params={"id": f"eq.{a_row['id']}"})

    assert len(found) == 1
    assert found[0]["title"] == "Original title"


def test_b_cannot_see_as_row(as_b, a_row):
    assert as_b.select(TABLE, params={"id": f"eq.{a_row['id']}"}) == []


def test_b_cannot_update_as_row(as_b, a_row):
    changed = as_b.update(
        TABLE, {"title": "Hijacked"}, params={"id": f"eq.{a_row['id']}"}
    )

    assert changed == []


def test_b_cannot_delete_as_row(as_b, a_row):
    assert as_b.delete(TABLE, params={"id": f"eq.{a_row['id']}"}) == []


def test_b_cannot_insert_a_row_claiming_a_as_owner(as_b):
    now = datetime.now(timezone.utc).isoformat()

    with pytest.raises(SupabasePermissionError):
        as_b.insert(
            TABLE,
            [
                {
                    "user_id": USER_A,
                    "fingerprint": f"forged-{uuid.uuid4()}",
                    "first_seen_at": now,
                    "last_seen_at": now,
                }
            ],
        )


def test_as_row_survives_every_attempt(as_a, as_b, a_row):
    as_b.update(TABLE, {"title": "Hijacked"}, params={"id": f"eq.{a_row['id']}"})
    as_b.delete(TABLE, params={"id": f"eq.{a_row['id']}"})

    survivor = as_a.select(TABLE, params={"id": f"eq.{a_row['id']}"})

    assert len(survivor) == 1
    assert survivor[0]["title"] == "Original title"
```

- [ ] **Step 3: Run it with no stack configured**

Run: `apps/job-hunter/.venv/bin/pytest tests/integration -q`
Expected: 6 skipped. This is what CI and other developers see when no stack is up.

- [ ] **Step 4: Run it against the live stack**

From the repository root:

```bash
supabase start
eval "$(supabase status -o env | sed 's/^/export /')"
python3 -c "import json;json.dump(json.load(open('supabase/signing_keys.json'))[0],open('/tmp/jwk-single.json','w'))"
export SUPABASE_TEST_URL="$API_URL"
export SUPABASE_TEST_PUBLISHABLE_KEY="$ANON_KEY"
export SUPABASE_TEST_SIGNING_KEY_B64="$(base64 < /tmp/jwk-single.json | tr -d '\n')"
cd apps/job-hunter && .venv/bin/pytest tests/integration -q
```

Expected: 6 passed.

If `test_b_cannot_insert_a_row_claiming_a_as_owner` fails with `SupabaseRequestError` rather than `SupabasePermissionError`, read the status code in the message: PostgREST maps the policy violation (SQLSTATE 42501) to 403, but confirm rather than assume, and adjust the client's mapping if the live stack disagrees.

- [ ] **Step 5: Correct the guidance this test sits against**

`apps/job-hunter/AGENTS.md:24` currently says no Python may be written against the Postgres tables outside issue #70. This test does write to `job_hunter_jobs`, so the rule needs to say what is actually true. Replace that sentence with:

```markdown
   Its migrations already define Job Hunter's Postgres tables (`public.job_hunter_*`, see
   `supabase/migrations/202609060002_job_hunter_discovery_state.sql`), but Job Hunter's runtime
   still reads and writes SQLite until #70 ports the store. Application code must not target
   those tables outside that ticket. The one exception is
   `tests/integration/test_supabase_isolation.py`, which writes and deletes a throwaway row in
   a local stack to prove the row-level security policies hold — that proof is #69's acceptance
   criterion.
```

- [ ] **Step 6: Run the full suite**

Run: `apps/job-hunter/.venv/bin/pytest -q`
Expected: all pass, integration tests skipped unless the stack variables are exported

- [ ] **Step 7: Commit**

```bash
git add apps/job-hunter/tests/integration apps/job-hunter/pyproject.toml apps/job-hunter/AGENTS.md
git commit -m "test: prove per-user isolation against live RLS policies"
```

---

### Task 8: CI job for the live isolation test

**Files:**
- Modify: `.github/workflows/job-hunter-ci.yml`

- [ ] **Step 1: Add the supabase path filter and the new job**

Replace `.github/workflows/job-hunter-ci.yml` with:

```yaml
name: Job Hunter CI
on:
  push:
    paths:
      - 'apps/job-hunter/**'
      - 'supabase/**'
      - '.github/workflows/job-hunter-ci.yml'
  pull_request:
    paths:
      - 'apps/job-hunter/**'
      - 'supabase/**'
      - '.github/workflows/job-hunter-ci.yml'
defaults:
  run:
    working-directory: apps/job-hunter
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
          cache-dependency-path: apps/job-hunter/pyproject.toml
      - run: pip install -e '.[test,webhook]'
      - run: pytest -q

  isolation:
    # Proves a per-user token cannot cross users, against real policies on a
    # throwaway local Supabase stack. Nothing touches the hosted project.
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
          cache-dependency-path: apps/job-hunter/pyproject.toml
      - uses: supabase/setup-cli@v1
        with:
          version: latest
      - name: Generate a throwaway signing key
        working-directory: .
        run: |
          supabase gen signing-key --algorithm ES256 > /tmp/jwk-single.json
          python3 -c "import json; json.dump([json.load(open('/tmp/jwk-single.json'))], open('supabase/signing_keys.json','w'))"
      - name: Start Supabase
        working-directory: .
        run: supabase start
      - run: pip install -e '.[test,webhook]'
      - name: Run the isolation test
        working-directory: .
        run: |
          eval "$(supabase status -o env | sed 's/^/export /')"
          export SUPABASE_TEST_URL="$API_URL"
          export SUPABASE_TEST_PUBLISHABLE_KEY="$ANON_KEY"
          export SUPABASE_TEST_SIGNING_KEY_B64="$(base64 -w0 < /tmp/jwk-single.json)"
          cd apps/job-hunter && pytest tests/integration -q
```

Two details that matter: the key is generated fresh inside the runner, so no secret is stored anywhere; and `base64 -w0` is the GNU form used on `ubuntu-latest`, unlike the macOS invocation in the local instructions.

- [ ] **Step 2: Verify the workflow parses**

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/job-hunter-ci.yml')); print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/job-hunter-ci.yml
git commit -m "ci: run the Supabase isolation test against a local stack"
```

- [ ] **Step 4: Confirm on the pull request**

The job runs when the PR opens in Task 9. If `supabase start` times out or the stack rejects the generated key, that is the same failure mode Task 1 gates on — report it rather than weakening the test.

---

### Task 9: Documentation and pull request

**Files:**
- Modify: `apps/job-hunter/AGENTS.md` (required secrets section, around line 146)

- [ ] **Step 1: Document the new secrets**

In the "Required secrets/env" section of `apps/job-hunter/AGENTS.md`, append:

```markdown
Not yet required by any runtime path, but needed once #70 ports the store to Postgres:
`JOB_HUNTER_USER_ID`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64`.
The last is the private JWK of the project's ES256 signing key and can mint a token for any
user — it is the most sensitive secret the platform has. See
`docs/superpowers/specs/2026-09-06-job-hunter-per-user-jwt-design.md`.
```

- [ ] **Step 2: Run the full suite one last time**

Run: `apps/job-hunter/.venv/bin/pytest -q` from `apps/job-hunter`
Expected: all pass. Record the exact count for the PR.

- [ ] **Step 3: Commit and push**

```bash
git add apps/job-hunter/AGENTS.md
git commit -m "docs: record the Supabase secrets Job Hunter will need"
git push -u origin feat/job-hunter-per-user-jwt
```

- [ ] **Step 4: Open the pull request**

Use `.github/PULL_REQUEST_TEMPLATE.md`. Fill Scope with `apps/job-hunter`, `supabase`, and CI. Put the real test counts in Testing. Under "Notes for the reviewer", state explicitly — `CONTRIBUTING.md` requires it — that the following must be applied outside git before anything runs against the hosted project:

1. Generate an ES256 signing key, import it into the Supabase project as a standby key, then rotate it to active.
2. Set `JOB_HUNTER_USER_ID`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` and `SUPABASE_SIGNING_KEY_B64` as GitHub Actions secrets.
3. Confirm the #66 migration has been pushed to the hosted project.

Reference the issue with `Closes #69`.

---

## Notes for the executor

- **Task 1 is a real gate.** If the local stack will not accept an ES256 token, stop and report. Every later task assumes it does.
- **Do not touch `pipeline.py`, `cli.py` or `store.py`.** Nothing constructs a `SupabaseClient` in production code at the end of this plan, and that is correct — #70 wires it in.
- **Never print the signing key or a token**, including while debugging. If a test failure would be easier to diagnose with the token visible, print the claim set instead.

# Vercel Flask Deployment Config Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the root-level Flask webhook deploy as a real Vercel Python Function by explicitly selecting the Flask framework in repository configuration.

**Architecture:** Keep `main.py` as the Flask entrypoint and preserve its function settings. Add `framework: "flask"` to `vercel.json` so Vercel uses the Flask backend pipeline even though the project was initially imported with the dashboard preset **Other**.

**Tech Stack:** Python 3.12, Flask 3.1, pytest, Vercel Python Functions

**Spec:** `docs/superpowers/specs/2026-09-01-vercel-root-function-config-fix-design.md`

## Global Constraints

- Do not change Telegram callback behavior.
- Do not change SQLite/GitHub artifact persistence.
- Do not add Supabase runtime dependencies.
- Keep root `main.py` as the Flask entrypoint.
- Keep `pip install -e '.[webhook]'` as the Vercel install command.
- Keep the existing `main.py` function duration at 30 seconds.

---

### Task 1: Lock the corrected Flask deployment shape

**Files:**
- Modify: `tests/test_vercel_config.py`

**Interfaces:**
- Consumes: repository-root `vercel.json`
- Produces: regression coverage for framework detection and root Flask function configuration

- [ ] **Step 1: Replace the earlier regression with the desired Flask config assertions**

Use:

```python
import json
from pathlib import Path


def test_vercel_config_declares_flask_root_entrypoint():
    config = json.loads(Path("vercel.json").read_text())

    assert config["framework"] == "flask"
    assert config["installCommand"] == "pip install -e '.[webhook]'"
    assert config["functions"]["main.py"]["maxDuration"] == 30
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
pytest -q tests/test_vercel_config.py
```

Expected: FAIL because the current intermediate config has neither `framework: "flask"` nor `functions.main.py`.

- [ ] **Step 3: Commit the corrected regression test**

```bash
git add tests/test_vercel_config.py
git commit -m "test: require Vercel Flask framework config"
```

---

### Task 2: Declare Flask explicitly and restore root function settings

**Files:**
- Modify: `vercel.json`
- Test: `tests/test_vercel_config.py`
- Test: `tests/test_vercel_entrypoint.py`

**Interfaces:**
- Preserves: `main.py` Flask app and webhook install command
- Produces: repository-controlled Flask framework detection

- [ ] **Step 1: Replace `vercel.json` with the supported Flask configuration**

Use exactly:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "framework": "flask",
  "installCommand": "pip install -e '.[webhook]'",
  "functions": {
    "main.py": {
      "maxDuration": 30,
      "excludeFiles": "{tests/**,.superpowers/**,docs/**,var/**}"
    }
  }
}
```

- [ ] **Step 2: Run focused tests and confirm GREEN**

Run:

```bash
pytest -q tests/test_vercel_config.py tests/test_vercel_entrypoint.py
```

Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run:

```bash
pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 4: Commit**

```bash
git add vercel.json
git commit -m "fix: declare Vercel Flask framework"
```

---

### Task 3: Verify the real Vercel preview and document the lesson

**Files:**
- Modify: `docs/telegram-job-navigator.md`

**Interfaces:**
- Documents: Vercel project preset, repository override, and observed deployment failure modes

- [ ] **Step 1: Update deployment troubleshooting documentation**

Record:

- the Vercel project may be imported with **Other**, but `vercel.json` must explicitly declare `"framework": "flask"`;
- root `main.py` is the supported Flask entrypoint;
- removing the `functions.main.py` mapping without enabling Flask can yield an empty READY deployment where `/health` returns 404;
- the successful verification criterion is a Vercel preview where `/health` returns `{"ok": true}`.

- [ ] **Step 2: Commit documentation**

```bash
git add docs/telegram-job-navigator.md
git commit -m "docs: clarify Vercel Flask deployment detection"
```

- [ ] **Step 3: Verify branch CI**

Confirm GitHub Actions installs `.[test,webhook]` and `pytest -q` passes on the final branch head.

- [ ] **Step 4: Verify Vercel preview**

Confirm the branch preview reaches READY, its build logs show the Flask/Python build path rather than an empty ~175 ms output, and `GET /health` returns HTTP 200 with `{"ok": true}`.

- [ ] **Step 5: Open PR**

Open the fix branch against `main` with the two observed failure modes and successful preview verification in the PR body.

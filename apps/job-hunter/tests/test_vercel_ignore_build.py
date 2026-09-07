"""The Vercel ignored-build-step helper must fail towards building.

A build that runs unnecessarily wastes minutes. A build wrongly skipped ships
nothing and is easy to miss, so every uncertain case here must build.
"""

import subprocess
from pathlib import Path

SCRIPT = Path("../../scripts/vercel-ignore-build.sh").resolve()

SKIP = 0
BUILD = 1


def _run(cwd, env, *pathspecs):
    return subprocess.run(
        ["sh", str(SCRIPT), *pathspecs],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    ).returncode


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "watched").mkdir()
    (tmp_path / "ignored").mkdir()
    (tmp_path / "watched" / "a.txt").write_text("one\n")
    (tmp_path / "ignored" / "b.txt").write_text("one\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()


def test_production_always_builds_even_when_nothing_changed(tmp_path):
    base = _repo(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "VERCEL_ENV": "production", "VERCEL_GIT_PREVIOUS_SHA": base}

    assert _run(tmp_path, env, "watched") == BUILD


def test_missing_previous_sha_builds(tmp_path):
    _repo(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "VERCEL_ENV": "preview"}

    assert _run(tmp_path, env, "watched") == BUILD


def test_unreadable_previous_sha_builds(tmp_path):
    _repo(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin",
        "VERCEL_ENV": "preview",
        "VERCEL_GIT_PREVIOUS_SHA": "0" * 40,
    }

    assert _run(tmp_path, env, "watched") == BUILD


def test_change_outside_the_watched_paths_skips(tmp_path):
    base = _repo(tmp_path)
    (tmp_path / "ignored" / "b.txt").write_text("two\n")
    _git(tmp_path, "commit", "-aqm", "unrelated change")
    env = {"PATH": "/usr/bin:/bin", "VERCEL_ENV": "preview", "VERCEL_GIT_PREVIOUS_SHA": base}

    assert _run(tmp_path, env, "watched") == SKIP


def test_change_inside_the_watched_paths_builds(tmp_path):
    base = _repo(tmp_path)
    (tmp_path / "watched" / "a.txt").write_text("two\n")
    _git(tmp_path, "commit", "-aqm", "relevant change")
    env = {"PATH": "/usr/bin:/bin", "VERCEL_ENV": "preview", "VERCEL_GIT_PREVIOUS_SHA": base}

    assert _run(tmp_path, env, "watched") == BUILD

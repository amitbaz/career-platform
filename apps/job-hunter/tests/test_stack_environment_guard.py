"""The Job Hunter suite cannot silently lose its store-backed coverage.

These tests run a tiny nested pytest suite so they exercise the real plugin lifecycle and
terminal result. The probe suite has one ordinary test and one test that requests `_stack_env`;
it never connects to Supabase.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_REQUIRED_STACK_ENV = (
    "SUPABASE_TEST_URL",
    "SUPABASE_TEST_PUBLISHABLE_KEY",
    "SUPABASE_TEST_SIGNING_KEY_B64",
)
_ALLOW_MISSING_STACK_ENV = "JOB_HUNTER_ALLOW_MISSING_STACK"
_JOB_HUNTER_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stack_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start each harness test with no stack configuration or opt-out."""
    for name in (*_REQUIRED_STACK_ENV, _ALLOW_MISSING_STACK_ENV):
        monkeypatch.delenv(name, raising=False)
    yield


def _make_probe_suite(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        f"""
        import sys

        sys.path.insert(0, {_JOB_HUNTER_ROOT.as_posix()!r})
        from tests.conftest import _stack_env, pytest_addoption, pytest_sessionstart
        """
    )
    pytester.makepyfile(
        """
        def test_without_store():
            pass

        def test_with_store(_stack_env):
            pass
        """
    )


def _combined_output(result: pytest.RunResult) -> str:
    return "\n".join((*result.stdout.lines, *result.stderr.lines))


def test_missing_stack_fails_once_before_collection(
    pytester: pytest.Pytester,
    stack_environment: None,
) -> None:
    """Removing the session guard would restore the false-successful run."""
    _make_probe_suite(pytester)

    result = pytester.runpytest_subprocess("-q")

    output = _combined_output(result)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert output.count("local Supabase stack is not configured") == 1
    assert "JOB_HUNTER_ALLOW_MISSING_STACK=1" in output
    assert "--allow-missing-stack" in output
    for name in _REQUIRED_STACK_ENV:
        assert name in output
    assert "passed" not in output


@pytest.mark.parametrize("opt_out", ["environment", "flag"])
def test_explicit_opt_out_allows_a_fully_absent_stack(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    stack_environment: None,
    opt_out: str,
) -> None:
    """Removing either opt-out would make a deliberate unit-only run impossible."""
    _make_probe_suite(pytester)
    args = ["-q"]
    if opt_out == "environment":
        monkeypatch.setenv(_ALLOW_MISSING_STACK_ENV, "1")
    else:
        args.append("--allow-missing-stack")

    result = pytester.runpytest_subprocess(*args)

    result.assert_outcomes(passed=1, skipped=1)


@pytest.mark.parametrize("opt_out", ["environment", "flag"])
def test_partial_stack_fails_even_with_an_opt_out(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    stack_environment: None,
    opt_out: str,
) -> None:
    """Honoring an opt-out for a partial environment would hide configuration typos."""
    _make_probe_suite(pytester)
    monkeypatch.setenv("SUPABASE_TEST_URL", "http://configured.invalid")
    args = ["-q"]
    if opt_out == "environment":
        monkeypatch.setenv(_ALLOW_MISSING_STACK_ENV, "1")
    else:
        args.append("--allow-missing-stack")

    result = pytester.runpytest_subprocess(*args)

    output = _combined_output(result)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert output.count("local Supabase stack is partially configured") == 1
    assert "SUPABASE_TEST_URL" not in output
    assert "SUPABASE_TEST_PUBLISHABLE_KEY" in output
    assert "SUPABASE_TEST_SIGNING_KEY_B64" in output


def test_environment_opt_out_requires_exactly_one(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    stack_environment: None,
) -> None:
    """Accepting truthy spellings would let a typo disable store coverage."""
    _make_probe_suite(pytester)
    monkeypatch.setenv(_ALLOW_MISSING_STACK_ENV, "true")

    result = pytester.runpytest_subprocess("-q")

    output = _combined_output(result)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert output.count("local Supabase stack is not configured") == 1


def test_complete_stack_runs_the_store_backed_probe(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    stack_environment: None,
) -> None:
    """Making the guard over-broad would skip the store probe despite complete config."""
    _make_probe_suite(pytester)
    for name in _REQUIRED_STACK_ENV:
        monkeypatch.setenv(name, "configured")

    result = pytester.runpytest_subprocess("-q")

    result.assert_outcomes(passed=2)

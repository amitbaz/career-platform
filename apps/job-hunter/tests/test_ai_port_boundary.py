"""No module outside the adapter knows which AI provider is in use (#73).

A second provider must be a new file under `job_hunter/ai/` plus one changed
wiring line, not an edit to every call site. That property is only true while
nothing imports the adapter, so it is asserted rather than trusted.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "job_hunter"

#: The adapter itself, and the composition root that wires it up. Someone has
#: to name a provider once; `cli.py` is where, and it is the only place.
ALLOWED_TO_NAME_GEMINI = {"ai/gemini.py", "cli.py"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _source_files() -> list[Path]:
    return sorted(p for p in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_the_adapter_and_the_wiring_import_the_gemini_module():
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in _source_files()
        if str(path.relative_to(SOURCE_ROOT)) not in ALLOWED_TO_NAME_GEMINI
        and any(
            module == "job_hunter.ai.gemini" or module.endswith(".gemini")
            for module in _imported_modules(path)
        )
    ]

    assert offenders == []


def test_the_legacy_provider_specific_modules_are_gone():
    assert not (SOURCE_ROOT / "gemini.py").exists()
    assert not (SOURCE_ROOT / "gemini_usage.py").exists()


def test_no_module_reads_a_run_id_from_the_environment():
    """`GEMINI_RUN_ID` is gone; nothing quota-related may key off a run (#73).

    Only executable references count: `postgres_store.py` still names the
    variable in a docstring, explaining why the ledger's `run_id` column is now
    an inert annotation.
    """
    for path in _source_files():
        tree = ast.parse(path.read_text())
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        docstrings = set()
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(first.value.value)
        assert not {
            value for value in literals - docstrings if "GEMINI_RUN_ID" in value
        }, path

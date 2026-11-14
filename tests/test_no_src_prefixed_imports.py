"""Static guard: no module under ``src/`` may use a ``src.``-prefixed import.

The service runs with a flat layout (``PYTHONPATH=src``), so every module must
import its siblings top-level (``from tools.architecture.registry import ...``), never
``from src.tools.architecture.registry import ...``.  Under the production posture
(``PYTHONPATH=/app/src`` with the repo root NOT on ``sys.path``) a
``src.``-prefixed import raises ``ModuleNotFoundError: No module named 'src'``,
500ing any endpoint that reaches the offending module.

This check is a pure AST scan — no DB, no fixtures, deterministic — and walks
the whole ``src/`` tree so it also catches any future ``src.``-prefixed import.
"""

from __future__ import annotations

import ast
import pathlib

import tools

# Locate ``src/`` robustly via an installed package's ``__file__`` (NOT cwd):
# ``tools`` lives at ``src/tools/__init__.py`` → parent.parent is ``src/``.
SRC_DIR = pathlib.Path(tools.__file__).resolve().parent.parent


def _offenders() -> list[str]:
    hits: list[str] = []
    for py in SRC_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module
                if mod is not None and (mod == "src" or mod.startswith("src.")):
                    hits.append(f"{py}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name == "src" or name.startswith("src."):
                        hits.append(f"{py}:{node.lineno}")
    return hits


def test_no_src_prefixed_imports() -> None:
    assert SRC_DIR.name == "src", f"expected to resolve src/, got {SRC_DIR}"
    offenders = _offenders()
    assert not offenders, (
        "src.-prefixed imports break the prod PYTHONPATH=/app/src layout "
        "(ModuleNotFoundError: No module named 'src'). Use top-level imports "
        "instead. Offending file:lineno:\n" + "\n".join(offenders)
    )

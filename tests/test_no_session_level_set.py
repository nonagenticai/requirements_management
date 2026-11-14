"""No session-level ``SET`` in ``src/`` — this service runs behind pgbouncer TRANSACTION pooling.

A session-level GUC survives the transaction and is handed, with the pooled server
connection, to the NEXT client (incident 2026-09-22: one ``SET
default_transaction_read_only`` cost 15 procedure writes and 300 inserts elsewhere). Only
transaction-scoped forms are allowed: ``SET LOCAL ...``, ``SET TRANSACTION ...``,
``SET CONSTRAINTS ...``, ``set_config(name, value, true)``.

Static guard: every string in ``src/**/*.py`` is scanned via ``ast`` (Python comments are
not read; docstrings are skipped). A string is a ``str`` constant, an f-string with each
``{...}`` replaced by a placeholder, or a ``+`` chain of those (``"SET lock" + "_timeout=1"``
is folded before matching). SQL comments are stripped, the text is split on ``;`` and each
statement whose stripped text STARTS with ``SET`` is checked — anchoring on the start
excludes ``UPDATE t SET col = ...`` / ``ON CONFLICT DO UPDATE SET ...``. Also flagged:
``set_config(..., false)`` (the session-level spelling of the same thing).

Not caught: SQL assembled across separate variables or ``str.join``/``%``/``.format``,
SQLAlchemy constructs, and session GUCs set via connect-time ``options=-c ...`` (pgbouncer
handles startup parameters itself).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

_SESSION_SET = re.compile(
    r"SET\s+(?:SESSION\s+)?(?:"
    r"(?:ROLE|SESSION\s+AUTHORIZATION|TIME\s+ZONE|CHARACTERISTICS)\b"
    r'|"[^"]+"\s*(?:=|\bTO\b)'  # quoted GUC name
    r"|(?!LOCAL\b)(?!CONSTRAINTS\b)(?!TRANSACTION\b)(?!SESSION\b)[a-z_][a-z_.]*\s*(?:=|\bTO\b)"
    r")",
    re.IGNORECASE,
)
_SET_STATEMENT = re.compile(r"SET\s", re.IGNORECASE)
_SET_LOCAL = re.compile(r"SET\s+LOCAL\b", re.IGNORECASE)
_SESSION_SET_CONFIG = re.compile(r"set_config\s*\(.*?,\s*false\s*\)", re.IGNORECASE | re.DOTALL)
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_PLACEHOLDER = "x"

# "path:line" -> reason. A legitimate session-level SET must be listed here WITH the reason
# it is safe behind transaction pooling. Empty on purpose.
ALLOWLIST: dict[str, str] = {}

# Vacuity floor: the scan must actually see this many files and transaction-scoped SETs.
MIN_FILES_SCANNED = 100
# Counts SET LOCAL statements only, so unrelated strings like ``ondelete="SET NULL"`` cannot
# keep the floor satisfied after the matcher goes blind.
MIN_SET_STATEMENTS_SEEN = 2  # services/idempotency.py: SET LOCAL lock_timeout (claim + reset)


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            ids.add(id(node.value))
    return ids


def _text(node: ast.AST) -> str | None:
    """The string a node evaluates to, with ``{...}`` placeholders; None if not a string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else _PLACEHOLDER
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def scan(root: Path) -> tuple[list[str], int, int]:
    """(offenders as ``path:line: text``, files scanned, SET LOCAL statements seen)."""
    offenders: set[str] = set()
    files = seen = 0
    for path in sorted(root.rglob("*.py")):
        files += 1
        tree = ast.parse(path.read_text(), filename=str(path))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            text = _text(node)
            if text is None:
                continue
            where = f"{path.relative_to(root.parent)}:{getattr(node, 'lineno', 0)}"
            if where in ALLOWLIST:
                continue
            if _SESSION_SET_CONFIG.search(text):
                offenders.add(f"{where}: {text.strip()!r}")
                continue
            # Constants inside an f-string / concatenation are also walked on their own;
            # count SET LOCAL once, from the outermost node that yields it.
            for stmt in _SQL_COMMENT.sub(" ", text).split(";"):
                stmt = stmt.strip()
                if not _SET_STATEMENT.match(stmt):
                    continue
                if _SET_LOCAL.match(stmt) and isinstance(node, ast.Constant):
                    seen += 1
                if _SESSION_SET.match(stmt):
                    offenders.add(f"{where}: {stmt!r}")
    return sorted(offenders), files, seen


def test_src_has_no_session_level_set():
    offenders, files, seen = scan(SRC)
    assert files >= MIN_FILES_SCANNED, f"scanned only {files} files under {SRC}"
    assert seen >= MIN_SET_STATEMENTS_SEEN, f"saw only {seen} SET statements: guard is blind"
    assert not offenders, (
        "session-level SET leaks across pgbouncer transaction pooling — use SET LOCAL "
        "inside the transaction (or set_config(..., true)):\n" + "\n".join(offenders)
    )


def test_allowlist_entries_still_exist():
    for where in ALLOWLIST:
        path, line = where.rsplit(":", 1)
        assert (SRC.parent / path).is_file() and int(line) > 0, f"stale allowlist entry {where}"


def test_guard_flags_session_level_forms(tmp_path):
    """Positive controls: the matcher fires on each session-level spelling."""
    pkg = tmp_path / "src"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        "from sqlalchemy import text\n"
        "A = text(\"SET lock_timeout = '5s'\")\n"
        "B = f\"SET statement_timeout TO '{1}s'\"\n"
        "C = 'set default_transaction_read_only = on'\n"
        "D = 'BEGIN; SET SESSION app.current_tenant = 1'\n"
        "E = 'SET ROLE requirements_app'\n"
        "F = \"SELECT set_config('app.current_tenant', '1', false)\"\n"
        "G = f\"SET {'lock_timeout'} = 1\"\n"
        "H = 'SET TIME ZONE ' + \"'UTC'\"\n"
        "I = 'SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY'\n"
        "J = '-- reset\\nSET x = 1'\n"
        "K = \"SELECT set_config('a', format('%s', 1), false)\"\n"
        "L = 'SET \"app.tenant\" = 1'\n"
        "M = 'SET lock' + '_timeout=1'\n"
    )
    (pkg / "good.py").write_text(
        '"""Set it to the default: a docstring, not SQL."""\n'
        "A = \"SET LOCAL lock_timeout = '5s'\"\n"
        "B = 'SET LOCAL lock_timeout = DEFAULT'\n"
        "C = 'UPDATE t SET col = 1 WHERE id = 2'\n"
        "D = 'INSERT INTO t VALUES (1) ON CONFLICT (id) DO UPDATE SET col = 1'\n"
        "E = 'SET TRANSACTION ISOLATION LEVEL SERIALIZABLE'\n"
        "F = 'SET CONSTRAINTS ALL DEFERRED'\n"
        "G = \"SELECT set_config('statement_timeout', %s, true)\"\n"
        "H = 'Set them via env vars on the Deployment.'\n"
        "I = 'ON DELETE SET NULL'\n"
        "J = f\"UPDATE t SET {'col'} = 1\"\n"
        "K = \"SELECT set_config('a', format('%s', 1), true)\"\n"
    )
    offenders, files, _seen = scan(pkg)
    assert files == 2
    flagged = sorted({int(o.split(":", 2)[1]) for o in offenders})
    assert all(o.startswith("src/bad.py:") for o in offenders), offenders
    assert flagged == list(range(2, 15)), offenders


def test_vacuity_floor_ignores_set_null(tmp_path):
    """``ondelete="SET NULL"`` strings must not count toward the SET LOCAL floor."""
    pkg = tmp_path / "src"
    pkg.mkdir()
    (pkg / "m.py").write_text("A = 'SET NULL'\nB = 'SET NULL'\nC = 'SET LOCAL x = 1'\n")
    _offenders, _files, seen = scan(pkg)
    assert seen == 1

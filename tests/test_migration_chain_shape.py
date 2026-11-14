"""The alembic chain: one head, linear, every revision id ``YYYY_MM_DD_NNNN``.

The id is what ``alembic_version`` stores and every ``down_revision`` names; a descriptive
suffix belongs in the FILE name only (``2026_10_27_0072`` was formerly
``2026_10_27_0072_org``).
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"\d{4}_\d{2}_\d{2}_\d{4}")


def _script() -> ScriptDirectory:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_one_head_and_a_linear_chain() -> None:
    script = _script()
    assert len(script.get_heads()) == 1, script.get_heads()
    revs = list(script.walk_revisions())
    for rev in revs:
        assert not isinstance(rev.down_revision, tuple), f"merge revision {rev.revision}"


def test_every_revision_id_is_a_bare_dated_sequence() -> None:
    bad = []
    for rev in _script().walk_revisions():
        path = Path(rev.path).name
        if not ID.fullmatch(rev.revision) or not path.startswith(rev.revision + "_"):
            bad.append((rev.revision, path))
    assert bad == []

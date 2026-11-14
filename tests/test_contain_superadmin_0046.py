"""0046 must drop the 0045 super-admin policy from EVERY table 0045 created it on.

A table missed here would keep the tenant-GUC-only cross-tenant read open on
that table (see the 0046 module docstring for the measured exposure).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_0046_drops_exactly_the_0045_policy_on_every_0045_table():
    m45 = _load("2026_10_01_0045_system_superadmin_read_policy")
    m46 = _load("2026_10_02_0046_drop_system_superadmin_read")
    assert m46.down_revision == m45.revision
    assert m46.POLICY == m45.POLICY == "system_superadmin_read"
    assert tuple(m46.TABLES) == tuple(m45.TABLES)


def test_0046_upgrade_only_drops(monkeypatch):
    m46 = _load("2026_10_02_0046_drop_system_superadmin_read")
    executed: list[str] = []
    monkeypatch.setattr(m46.op, "execute", executed.append)
    m46.upgrade()
    assert len(executed) == len(m46.TABLES)
    assert all(s.startswith("DROP POLICY IF EXISTS system_superadmin_read ON ") for s in executed)
    assert not any("CREATE" in s for s in executed)

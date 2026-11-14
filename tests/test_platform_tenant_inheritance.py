"""Only PLATFORM tenants inherit system rows (owner rule 2026-09-30).

System + improvement data live on their own tenants and must NEVER be visible
to a customer tenant. ``repositories.visibility.scope`` / ``is_inherited_visible``
gate the system widening on ``settings.platform_tenant_ids``
(env ``PLATFORM_TENANT_IDS``). HTTP-level cells live in
``test_visibility_matrix.py`` and ``test_system_tenant_inheritance.py``; this
file pins the setting and the pure helpers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from models.principles import Principle
from repositories.visibility import is_inherited_visible, is_platform_tenant, scope
from settings import Settings, get_settings
from tests.tenants import T

pytestmark = [pytest.mark.tenant_isolation, pytest.mark.security]

SYSTEM = get_settings().system_tenant_id
CUSTOMER = T("platform-inheritance-customer")
N8N_DEV = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"

EXPECTED_DEFAULT = frozenset(
    {
        "0195f48b-d000-7000-8000-000000000001",
        "0195f48b-d000-7000-8000-000000000002",
        "0195f48b-d000-7000-8000-000000000003",
        "0195f48b-d000-7000-8000-000000000004",
        "0195f48b-d000-7000-8000-000000000005",
        N8N_DEV,
    }
)


# ── (d) the setting ─────────────────────────────────────────────────────────


def test_default_platform_set_is_the_six_platform_tenants(monkeypatch) -> None:
    monkeypatch.delenv("PLATFORM_TENANT_IDS", raising=False)
    assert Settings().platform_tenant_id_set == EXPECTED_DEFAULT


def test_env_override_replaces_the_set(monkeypatch) -> None:
    other = T("platform-inheritance-override")
    monkeypatch.setenv("PLATFORM_TENANT_IDS", f" {SYSTEM.upper()} , {other} ,")
    assert Settings().platform_tenant_id_set == frozenset({SYSTEM, other})


def test_env_override_rejects_a_non_uuid(monkeypatch) -> None:
    monkeypatch.setenv("PLATFORM_TENANT_IDS", f"{SYSTEM},n8n-dev")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["", "   ", ",", " , ,, "])
def test_env_override_rejects_an_empty_set(monkeypatch, value: str) -> None:
    """An explicitly empty value must crash startup, not yield an empty set
    (which would silently cut the n8n conveyor off from system rows)."""
    monkeypatch.setenv("PLATFORM_TENANT_IDS", value)
    with pytest.raises(ValidationError, match="PLATFORM_TENANT_IDS is set but empty"):
        Settings()


def test_unset_env_still_gives_the_default_six(monkeypatch) -> None:
    monkeypatch.delenv("PLATFORM_TENANT_IDS", raising=False)
    monkeypatch.delenv("platform_tenant_ids", raising=False)
    s = Settings()
    assert s.platform_tenant_id_set == EXPECTED_DEFAULT
    assert len(s.platform_tenant_id_set) == 6


# ── the helpers ─────────────────────────────────────────────────────────────


def test_is_platform_tenant() -> None:
    assert is_platform_tenant(N8N_DEV) is True
    assert is_platform_tenant(N8N_DEV.upper()) is True
    assert is_platform_tenant(CUSTOMER) is False
    assert is_platform_tenant(None) is False
    # Explicit set overrides the setting.
    assert is_platform_tenant(CUSTOMER, [CUSTOMER]) is True
    assert is_platform_tenant(N8N_DEV, []) is False


def _sys_row(status: str = "PUBLISHED") -> SimpleNamespace:
    return SimpleNamespace(tenant_id=SYSTEM, visibility="tenant", version_status=status)


def test_is_inherited_visible_fences_customers() -> None:
    row = _sys_row()
    # (a) customer: never
    assert is_inherited_visible(row, tenant_id=CUSTOMER, system_tenant_id=SYSTEM) is False
    # (b) platform: yes
    assert is_inherited_visible(row, tenant_id=N8N_DEV, system_tenant_id=SYSTEM) is True
    # (c) system: own row, unchanged
    assert is_inherited_visible(row, tenant_id=SYSTEM, system_tenant_id=SYSTEM) is True
    # A platform tenant still never sees an unpublished system row.
    draft = _sys_row("DRAFT")
    assert is_inherited_visible(draft, tenant_id=N8N_DEV, system_tenant_id=SYSTEM) is False


def _widens(tenant: str) -> bool:
    """True if ``scope(include_system=True)`` admits the system tenant for ``tenant``."""
    stmt = scope(
        select(Principle),
        Principle,
        tenant_id=tenant,
        subject_id=tenant,
        include_system=True,
        system_tenant_id=SYSTEM,
    )
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    return "PUBLISHED" in str(compiled)


def test_scope_widens_only_for_platform_tenants() -> None:
    assert _widens(CUSTOMER) is False
    assert _widens(N8N_DEV) is True
    for tid in EXPECTED_DEFAULT - {SYSTEM}:
        assert _widens(tid) is True, tid
    # The system tenant reads its own rows; no widening needed.
    assert _widens(SYSTEM) is False

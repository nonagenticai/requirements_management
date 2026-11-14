"""Stable UUID tenant ids for tests.

``tenant_id`` is a real ``uuid`` column (Phase 4 of the UUIDv7 plan declared in
``alembic/versions/2026_04_27_0019_add_tenant_name_column.py``). A test can no
longer seed a readable label like ``"user-123"`` — Postgres rejects it with
``invalid input syntax for type uuid``.

``T("user-123")`` maps that label to a stable UUID, so the label stays visible
where it matters (in the test) while the value the database sees is a UUID. The
mapping is deterministic and 1:1, so two tests that used the same label still
share a tenant, and every cross-tenant isolation assertion keeps exactly the
meaning it had before the flip.

This mirrors what the production migration does to the 11 orphan tenant values
it found: same namespace, same derivation, so a label maps to the same UUID in
a test as it would in the data repair.
"""

import uuid

# Fixed namespace — never change it: the mapping must stay stable across runs,
# across branches, and between this module and migration
# ``2026_08_24_0042_tenant_id_is_a_uuid``.
TENANT_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


def T(label: str) -> str:
    """The stable UUID for a readable tenant label.

    Returns a ``str`` (not a ``uuid.UUID``): the columns are
    ``sa.Uuid(as_uuid=False)``, so the whole Python side of this service still
    speaks ``str`` — headers, Pydantic schemas, ``get_tenant_id()`` and the RLS
    GUC all stay unchanged.
    """
    return str(uuid.uuid5(TENANT_NAMESPACE, label))

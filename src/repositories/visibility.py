"""Per-user-within-tenant visibility helpers.

Two scoping axes:
- tenant_id: the organization that owns the data (a ``uuid`` column — a tenant NAME is an
  alias resolved at the boundary by ``src/auth/tenant_resolution.py``, never stored here)
- subject_id: the user (Subject UUID from licenses) within that tenant

Rows have a `visibility` column ('tenant' default | 'user'). List queries
return tenant-visible rows + the caller's own user-visible rows. Mutations
on user-visible rows are gated to the creator.

System-tenant inheritance (READ paths only — owner design,
n8n_custom_nodes ``AA_IMPLEMENTATION/SPEC.md:106-118``, narrowed by the owner
rule of 2026-09-30: system + improvement data must NEVER be visible to a
customer tenant, so ONLY a PLATFORM tenant — ``settings.platform_tenant_ids`` —
inherits; a customer tenant reads its own rows only, whatever the caller passes):
When ``include_system=True``, the caller is a platform tenant and
``system_tenant_id`` differs from the active tenant, a SELECT widens to also admit rows owned by the system tenant that are
``visibility='tenant'`` AND PUBLISHED (``version_status='PUBLISHED'``). A
system DRAFT/DEPRECATED row is never visible to another tenant. A model with
no ``version_status`` column has no publication state, so it inherits NOTHING
(``include_system`` is a no-op for it) — failing closed rather than leaking
system drafts. Where the model has a ``slug`` (the per-tenant natural key,
``uq_<table>_tenant_slug``), a caller-visible own-tenant row SHADOWS the system
row of the same slug — done in SQL (``NOT EXISTS``) so pagination and counts
stay correct. Mutations never widen: callers MUST NOT pass
``include_system=True`` on a lookup that feeds ``can_mutate``, and
``can_mutate`` independently refuses any row outside the active tenant.

No super-admin read (user ruling 2026-10-02): the system tenant gets the
ordinary own-tenant scope like every other tenant; nothing lets one tenant
read another tenant's rows except the platform-gated inheritance of
system-PUBLISHED rows above. The former super-admin branch (TENANCY §2.1,
#187/0045) was a live confidentiality regression and is removed; see
``tests/test_keepout_no_cross_tenant_read.py``.

Template: ``procedure_management/src/repositories/visibility.py`` (which
shadows at the application layer; this service shadows in SQL).
"""

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.expression import Select

from settings import get_settings

PUBLISHED = "PUBLISHED"


def same_tenant(a: Any, b: Any) -> bool:
    """Tenant equality as the uuid COLUMN sees it (case/format-insensitive).

    A plain ``str`` compare would call ``0195F48B-…`` and ``0195f48b-…``
    different tenants while the SQL ``WHERE tenant_id = …`` calls them equal —
    that mismatch would 404 a legitimate mutation.
    """
    if a is None or b is None:
        return False
    try:
        return UUID(str(a)) == UUID(str(b))
    except ValueError:
        return str(a) == str(b)


def _own_clause(model: Any, *, tenant_id: str, subject_id: str) -> ColumnElement[bool]:
    return and_(
        model.tenant_id == tenant_id,
        or_(
            model.visibility == "tenant",
            and_(model.visibility == "user", model.subject_id == subject_id),
        ),
    )


def is_platform_tenant(tenant_id: Any, platform_tenant_ids: Collection[str] | None = None) -> bool:
    """True if ``tenant_id`` may inherit system rows (owner rule 2026-09-30).

    System + improvement data must NEVER be visible to a customer tenant, so the
    system widening is gated on membership of the PLATFORM tenant set
    (``settings.platform_tenant_ids``, env ``PLATFORM_TENANT_IDS``). ``None``
    reads the setting; tests may pass an explicit collection. Fails CLOSED: an
    unparseable or absent tenant is never a platform tenant.
    """
    if tenant_id is None:
        return False
    ids = (
        get_settings().platform_tenant_id_set
        if platform_tenant_ids is None
        else platform_tenant_ids
    )
    return any(same_tenant(tenant_id, p) for p in ids)


def inherits_system(model: Any) -> bool:
    """True if ``model`` carries a publication state, i.e. can inherit system rows."""
    return hasattr(model, "version_status")


def system_clause(
    model: Any, *, tenant_id: str, subject_id: str, system_tenant_id: str
) -> ColumnElement[bool]:
    """Predicate admitting the system tenant's PUBLISHED, tenant-visible rows.

    Includes the slug-shadowing ``NOT EXISTS`` when the model has a ``slug``.
    Only valid for models where ``inherits_system(model)`` is True.
    """
    clause = and_(
        model.tenant_id == system_tenant_id,
        model.visibility == "tenant",
        model.version_status == PUBLISHED,
    )
    if hasattr(model, "slug"):
        shadow = aliased(model)
        clause = and_(
            clause,
            ~exists().where(
                shadow.slug == model.slug,
                _own_clause(shadow, tenant_id=tenant_id, subject_id=subject_id),
            ),
        )
    return clause


def scope(
    stmt: Select,
    model: Any,
    *,
    tenant_id: str,
    subject_id: str,
    include_system: bool = False,
    system_tenant_id: str | None = None,
    platform_tenant_ids: Collection[str] | None = None,
) -> Select:
    """Narrow a SELECT to rows visible to (tenant_id, subject_id).

    Returns:
        - all rows in the tenant with visibility='tenant', plus
          rows in the tenant with visibility='user' that the caller owns,
          plus — (when ``include_system``, the caller is a PLATFORM tenant,
          ``system_tenant_id != tenant_id`` and the model has
          ``version_status``) the system tenant's tenant-visible PUBLISHED rows
          not shadowed by a caller-visible own row of the same slug.

    A customer (non-platform) tenant NEVER widens, whatever ``include_system``
    says — the gate lives here so no caller can forget it.

    The SYSTEM tenant gets the ordinary own-tenant scope and NOTHING more: it
    never reads another tenant's rows (user ruling 2026-10-02; the former
    super-admin short-circuit is removed). Every returned statement carries a
    ``tenant_id`` predicate.
    """
    own = _own_clause(model, tenant_id=tenant_id, subject_id=subject_id)
    if (
        include_system
        and system_tenant_id
        and not same_tenant(system_tenant_id, tenant_id)
        and inherits_system(model)
        and is_platform_tenant(tenant_id, platform_tenant_ids)
    ):
        return stmt.where(
            or_(
                own,
                system_clause(
                    model,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    system_tenant_id=system_tenant_id,
                ),
            )
        )
    return stmt.where(own)


def is_inherited_visible(
    row: Any,
    *,
    tenant_id: str,
    system_tenant_id: str | None,
    platform_tenant_ids: Collection[str] | None = None,
) -> bool:
    """Python mirror of the system half of ``scope`` for a row already loaded by id.

    For endpoint checks that load with ``session.get`` (no SELECT to scope):
    True if ``row`` is the active tenant's (the system tenant is NOT a
    super-admin: it sees only its own rows), or — for a PLATFORM
    caller only — a PUBLISHED tenant-visible system row. Does NOT apply slug
    shadowing — an id is an explicit reference.
    """
    if same_tenant(row.tenant_id, tenant_id):
        return True
    return bool(
        system_tenant_id
        and is_platform_tenant(tenant_id, platform_tenant_ids)
        and same_tenant(row.tenant_id, system_tenant_id)
        and getattr(row, "visibility", None) == "tenant"
        and getattr(row, "version_status", None) == PUBLISHED
    )


def is_visible_to(
    row: Any,
    *,
    tenant_id: str,
    subject_id: str,
    system_tenant_id: str | None,
    platform_tenant_ids: Collection[str] | None = None,
) -> bool:
    """``is_inherited_visible`` plus the SUBJECT half of ``_own_clause``.

    ``is_inherited_visible`` admits every same-tenant row, including another
    subject's ``visibility="user"`` rows, so a reference check built on it lets
    a caller point a relationship at a private element it cannot read
    (finding A04). This is the Python mirror of ``scope`` for one row loaded by
    id: own tenant AND (tenant-visible OR the caller's own private row), or the
    platform system-inheritance path.
    """
    if same_tenant(row.tenant_id, tenant_id):
        visibility = getattr(row, "visibility", "tenant")
        if visibility == "tenant":
            return True
        return visibility == "user" and str(getattr(row, "subject_id", "")) == str(subject_id)
    return is_inherited_visible(
        row,
        tenant_id=tenant_id,
        system_tenant_id=system_tenant_id,
        platform_tenant_ids=platform_tenant_ids,
    )


def can_mutate(row: Any, *, subject_id: str, tenant_id: str) -> bool:
    """True if caller may mutate row.

    - never a row outside the active tenant — in particular never an inherited
      system row, even if some read path widened to load it;
    - tenant-visible: any tenant member;
    - user-visible: only the creator.
    """
    if not same_tenant(row.tenant_id, tenant_id):
        return False
    if row.visibility == "tenant":
        return True
    return bool(row.subject_id == subject_id)

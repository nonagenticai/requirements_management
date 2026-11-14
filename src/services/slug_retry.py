"""Shared concurrent-slug-collision recovery primitives.

All of this service's get-or-create surfaces optimistically INSERT a row whose
per-tenant slug is derived from a base (a title, or an edge tuple) with a
check-then-insert pre-pass. Two concurrent creates that derive the SAME base
slug from DIFFERENT identities both pass their pre-check and race the plain
``uq_<table>_tenant_slug`` unique constraint; the loser's identity re-SELECT
finds nothing (it was NOT an identity duplicate) and, without recovery, the
``IntegrityError`` propagates as an HTTP 500.

The fix (first shipped for ``EntityService.get_or_create`` in PR #80) is a
BOUNDED slug-regeneration retry: on an ``IntegrityError`` whose identity
re-SELECT is empty, regenerate the slug on a fresh create and retry — bounded so
a genuinely-fatal constraint still exhausts and re-raises (today's behaviour, no
regression). This module holds the pieces every site shares — the bound, the
``(tenant_id, slug)`` constraint-name introspection, and the "is this violation
some OTHER known constraint (→ fatal)" fast-path predicate — so the loop body
(site-specific create / identity re-SELECT / update) is the only thing each
call site writes itself.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

# Bounded retries for the concurrent-slug-collision recovery. Two concurrent
# same-base-slug / different-identity creates both derive the same base slug;
# the loser collides on the plain uq_<table>_tenant_slug constraint (NOT the
# identity index), so its identity re-SELECT finds nothing. Regenerating the
# slug (a fresh create re-runs the site's slug generator, which now sees the
# committed winner and appends a suffix) and retrying resolves the collision;
# bounded so a genuinely-fatal constraint exhausts and re-raises.
MAX_SLUG_RETRIES = 3


def find_slug_constraint_name(model_class) -> str | None:
    """Return the name of the model's ``(tenant_id, slug)`` UniqueConstraint.

    Cannot be derived from ``__tablename__`` (e.g. ``architecture_elements`` →
    ``uq_arch_elements_tenant_slug``), so introspect the table's constraints.
    Returns None when slug uniqueness is expressed as a unique ``Index`` rather
    than a ``UniqueConstraint`` (then callers fall back to
    retry-on-empty-re-SELECT, which is self-sufficient).
    """
    for c in model_class.__table__.constraints:
        if isinstance(c, UniqueConstraint) and {col.name for col in c.columns} == {
            "tenant_id",
            "slug",
        }:
            return c.name
    return None


def violated_constraint(err) -> str | None:
    """Best-effort name of the constraint an IntegrityError violated.

    Optional fast-path only — psycopg's ``diag.constraint_name`` may be empty
    (returns None), in which case callers rely on the empty-re-SELECT
    discriminator instead.
    """
    return getattr(getattr(getattr(err, "orig", None), "diag", None), "constraint_name", None)


def is_fatal_non_slug_violation(err, slug_constraint_name: str | None) -> bool:
    """True when the IntegrityError names some OTHER known constraint (fatal).

    Used only in the empty-identity-re-SELECT branch as a fast-path: if the
    violation names a constraint that is NOT the model's ``(tenant_id, slug)``
    unique constraint, retrying with a fresh slug cannot help — it is genuinely
    fatal, so re-raise immediately. When the diag name is empty, or the model's
    slug constraint name could not be resolved, this returns False and the
    caller falls back to the bounded retry-on-empty-re-SELECT (self-sufficient).
    """
    name = violated_constraint(err)
    return name is not None and slug_constraint_name is not None and name != slug_constraint_name

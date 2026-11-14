"""A hard ceiling on how many rows a tool endpoint may load INTO memory.

WHY THIS MODULE EXISTS
----------------------
``routers/architecture_tools.py`` has two loaders — ``_load_elements`` (line 60)
and ``_load_relationships`` (line 131) — that issue an unfiltered
``select(Entity).where(tenant_id == …)`` with **no LIMIT**, hydrate every row
into a full ORM entity, and then project each one into a ``list[dict]``. Twelve
endpoints reach that pair. The peak resident set is therefore the ORM identity
map *plus* the dict projection *plus* whatever the analysis tool downstream
builds from it, and it is a function of tenant size, not of the request.

Measured on the largest live tenant: **2,703 architecture elements** and
**11,674 architecture relationships**. The pod is ``memory: 512Mi`` with
``replicas: 1``, so there is no second replica to absorb a kill and no headroom
to grow into. This is not hypothetical: an unbounded load on *this service*
already OOM-killed it once. PR #147 responded by bounding the **response** and
said, in as many words, that the **input** was left unbounded. This module is
the other half.

WHY THESE NUMBERS
-----------------
``DEFAULT_MAX_INPUT_ELEMENTS = 6000`` against 2,703 measured elements is
6000 / 2703 ≈ **2.2x** the largest tenant alive today.
``DEFAULT_MAX_INPUT_RELATIONSHIPS = 24000`` against 11,674 measured
relationships is 24000 / 11674 ≈ **2.1x**.

The arithmetic is spelled out so the next reader can re-derive it rather than
trust it. The point of the ~2x is that **nothing truncates on day one** — a cap
that fires the moment it ships is indistinguishable from an outage, and the
operator learns to raise it reflexively instead of reading it. It also means
the estate has to roughly double before anyone sees an ``InputTooLarge``, which
is enough runway to page a human rather than to surprise one. Both are
env-overridable (``TOOL_MAX_INPUT_ELEMENTS`` / ``TOOL_MAX_INPUT_RELATIONSHIPS``)
so raising the ceiling does not need a deploy — but raising it without also
raising the pod's ``memory`` limit just moves the failure from a 4xx back to an
OOM kill, which is the strictly worse of the two.

WHY THE CAP IS ON THE INPUT AND NOT ON THE RESPONSE
---------------------------------------------------
A response cap protects the *client*. It does not protect the *process*: the
rows are already resident by the time anything truncates the output. Only a
``LIMIT`` in the SQL keeps them out of the heap.

WHY ``fetch_bounded`` COSTS ONE QUERY, NOT TWO
-----------------------------------------------
The obvious shape — ``COUNT(*)``, then ``SELECT``— doubles the round trips on
every request in order to serve the case that never happens. Instead we ask for
``cap + 1`` rows: if the extra row comes back we know we are over the bound, and
only *then* do we spend a second query to learn the exact total (which is what
the error message needs in order to tell the operator how far over they are).
Under the cap — the common path, and the only path anyone runs today — it is
one query, exactly as before, with a ``LIMIT`` attached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Select

# Read at import time from the environment so the ceiling is a Deployment knob,
# not a redeploy. Module attributes rather than function defaults: a test (or an
# operator via env) can move them without a caller having frozen the value into
# its own signature at def time.
DEFAULT_MAX_INPUT_ELEMENTS: int = int(os.getenv("TOOL_MAX_INPUT_ELEMENTS", "6000"))
DEFAULT_MAX_INPUT_RELATIONSHIPS: int = int(os.getenv("TOOL_MAX_INPUT_RELATIONSHIPS", "24000"))


class InputTooLarge(ValueError):
    """The tenant's graph is larger than this endpoint will load into memory.

    Raised — never returned — and deliberately a plain ``ValueError`` subclass
    with **no FastAPI import**: ``src/repositories/**`` stays framework-free and
    the router owns the HTTP mapping. Returning a dict with an ``"error"`` key
    instead would hand the caller **HTTP 200 with a failure inside the body**,
    which every ``raise_for_status``, every success-rate panel and every retry
    policy reads as a successful analysis.

    Attributes
    ----------
    label:
        What was being loaded, in the operator's vocabulary — e.g.
        ``"architecture elements"``. Appears verbatim in the message, so the
        reader does not have to guess which of two loaders refused.
    total:
        The exact number of rows the request would have loaded. Exact, not
        ``cap + 1``: knowing you are 3 over the bound and knowing you are 30,000
        over it call for completely different responses.
    limit:
        The ceiling that was applied.
    """

    def __init__(self, label: str, total: int, limit: int) -> None:
        self.label = label
        self.total = total
        self.limit = limit
        super().__init__(
            f"This request would load {total} {label} into memory; the limit is {limit}. "
            f"Refusing rather than loading the whole set — this service runs one replica "
            f"at 512Mi and an unbounded load on this path has already OOM-killed it once, "
            f"which takes out every other request in flight, not just this one. "
            f"Narrow the request (scope it to a single project rather than the whole "
            f"tenant, or split the analysis), or raise the matching TOOL_MAX_INPUT_* "
            f"environment variable — but raise the pod's memory limit with it, or the "
            f"only thing that changes is which failure you get."
        )


@dataclass(frozen=True)
class BoundedRows:
    """The outcome of a bounded fetch: the rows, and an honest account of them.

    ``truncated`` is the whole point of the type. A bare ``list`` cannot tell a
    caller apart from "there were exactly this many rows" and "there were more
    and you are holding a non-deterministic prefix", and a caller that cannot
    tell those apart will silently compute an analysis over a subset and report
    it as complete.

    Attributes
    ----------
    rows:
        At most ``cap`` rows. ORM entities for an entity ``SELECT``, ``Row``
        tuples for a columns-only one — see ``fetch_bounded``.
    returned:
        ``len(rows)``. Equals the cap exactly when ``truncated`` is true; that
        invariant is what lets ``raise_if_truncated`` report the limit without
        the dataclass having to carry it separately.
    total:
        The exact number of rows the unbounded query would have produced. Equal
        to ``returned`` when not truncated (no second query was needed to know
        that).
    truncated:
        True when the table held more than ``cap`` matching rows.
    label:
        Operator-facing name of what was loaded, carried through to the error.
    """

    rows: list[Any]
    returned: int
    total: int
    truncated: bool
    label: str

    def raise_if_truncated(self) -> None:
        """Refuse the request when the rows in hand are only a prefix.

        Separate from ``fetch_bounded`` on purpose. Truncation is not always an
        error — a caller that genuinely wants "the first N" is entitled to look
        at ``truncated`` and carry on. It is an error for the analysis tools,
        which draw conclusions about a whole architecture, and those callers say
        so by calling this.
        """
        if self.truncated:
            # ``returned`` IS the cap here — see the invariant on the field.
            raise InputTooLarge(self.label, self.total, self.returned)


def _selects_whole_entity(stmt: Select) -> bool:
    """True when ``stmt`` selects one mapped entity rather than columns.

    ``select(Model)`` must be consumed through ``.scalars()`` or every row comes
    back as a one-tuple; ``select(Model.a, Model.b)`` must NOT be, or the second
    column is silently dropped. The house pattern for the columns-only form is
    ``(await session.execute(stmt)).all()``, so that is the shape we preserve.

    Note that ``entity is not None`` alone does not discriminate: SQLAlchemy
    reports the same owning entity for ``select(Model.id)`` as for
    ``select(Model)``. What separates them is that only in the whole-entity case
    is the selected expression the entity itself.
    """
    descriptions = stmt.column_descriptions
    if len(descriptions) != 1:
        return False
    first = descriptions[0]
    entity = first.get("entity")
    return entity is not None and first.get("expr") is entity


async def fetch_bounded(
    session: AsyncSession,
    stmt: Select,
    *,
    cap: int,
    order_by: Any,
    label: str,
) -> BoundedRows:
    """Run ``stmt`` with a hard ``LIMIT``, reporting whether it truncated.

    One round trip on the common path. ``cap + 1`` rows are requested; if the
    extra one arrives it is discarded, ``truncated`` is set, and a second query
    counts the true total for the error message. At or under the cap there is no
    second query and ``total == returned``.

    ``order_by`` is REQUIRED, not optional. ``LIMIT`` without ``ORDER BY`` is a
    non-deterministic subset in Postgres — the planner is free to return
    different rows for the same query — so an unordered bound would turn "this
    tenant is too big" into the far worse "the same request answers differently
    each time". Pass a column that is unique or near-unique within the result.

    Args:
        session: The async session to execute on.
        stmt: The ``SELECT`` to bound. Must NOT already carry a ``LIMIT``; the
            caller's filtering (tenant scope, project scope) belongs here.
        cap: Maximum rows to load. Must be at least 1.
        order_by: Ordering expression applied before the ``LIMIT``.
        label: Operator-facing name of what is being loaded, e.g.
            ``"architecture elements"``. Used in ``InputTooLarge``.

    Returns:
        A ``BoundedRows`` carrying at most ``cap`` rows.

    Raises:
        ValueError: If ``cap`` is less than 1. A cap of 0 would load nothing and
            report every tenant as over the bound, which is a configuration
            mistake worth failing loudly on rather than serving.
    """
    if cap < 1:
        raise ValueError(f"cap must be at least 1, got {cap}")

    # +1 is the probe: it is the cheapest possible test for "is there more?",
    # and it is why the common path needs no COUNT.
    result = await session.execute(stmt.order_by(order_by).limit(cap + 1))
    rows: list[Any] = list(result.scalars().all() if _selects_whole_entity(stmt) else result.all())

    if len(rows) <= cap:
        return BoundedRows(
            rows=rows,
            returned=len(rows),
            total=len(rows),
            truncated=False,
            label=label,
        )

    # Over the bound. Drop the probe row and spend the second query — only here,
    # and only to make the refusal say how far over the caller actually is.
    rows = rows[:cap]
    # ``order_by(None)`` strips any ordering the caller put on ``stmt``: it
    # cannot change a count, and it makes Postgres skip a sort of the whole set.
    total = (
        await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
    ).scalar_one()

    return BoundedRows(
        rows=rows,
        returned=cap,
        total=int(total),
        truncated=True,
        label=label,
    )

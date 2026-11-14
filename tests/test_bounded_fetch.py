"""Behavioural lock on ``repositories.bounded.fetch_bounded``.

Pure-DB tests against real Postgres: seed rows, call ``fetch_bounded``, assert.
The whole value of this helper is in its edges — the off-by-one at exactly the
cap, and whether ``total`` is the TRUE count or an artefact of the ``cap + 1``
probe — so each edge gets its own test and no test asserts two of them.

WHY ``ArchitectureRelationship`` AND NOT SOME PURPOSE-BUILT FIXTURE TABLE
-------------------------------------------------------------------------
It is one of the two tables the bound actually exists to protect (the other is
``architecture_elements``; see ``routers/architecture_tools.py:60,131``), so the
test exercises the real shape rather than a stand-in. Of that pair it is much
the cheaper to seed: ``ArchitectureElement`` mixes in ``QualityMixin`` and
carries a persisted ``tsvector`` ``Computed`` column, so every seeded row costs
a ``to_tsvector`` call, while ``ArchitectureRelationship`` has neither and is a
plain insert of nine small columns.

The caps used here are single digits, not the production 6000 / 24000. The
helper's behaviour at a boundary does not depend on where the boundary is, and
seeding 6,001 rows to prove an off-by-one would buy nothing but minutes.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Select

from models.architecture import ArchitectureRelationship
from repositories.bounded import BoundedRows, InputTooLarge, fetch_bounded
from tests.tenants import T

_TENANT = T("bounded-fetch-tenant")
_TENANT_NAME = "Bounded Fetch Tenant"
_SUBJECT = "bounded-fetch-subject"
_LABEL = "architecture relationships"


async def _seed(session: AsyncSession, count: int) -> list[str]:
    """Insert ``count`` relationships and return their slugs in order.

    Slugs are zero-padded so lexical order is insertion order — that makes the
    ``order_by`` deterministic AND makes "which rows came back" checkable.
    """
    slugs = [f"bounded-rel-{i:04d}" for i in range(count)]
    session.add_all(
        [
            ArchitectureRelationship(
                slug=slug,
                tenant_id=_TENANT,
                tenant_name=_TENANT_NAME,
                subject_id=_SUBJECT,
                visibility="tenant",
                project_id="bounded-project",
                source_entity_type="architecture_elements",
                source_id=uuid.uuid4(),
                target_entity_type="architecture_elements",
                target_id=uuid.uuid4(),
                relationship_type="association",
                properties={},
            )
            for slug in slugs
        ]
    )
    await session.commit()
    return slugs


def _stmt() -> Select:
    """The tenant-scoped SELECT under test — an entity select, as the loaders use."""
    return select(ArchitectureRelationship).where(ArchitectureRelationship.tenant_id == _TENANT)


async def test_under_cap_is_not_truncated(test_session: AsyncSession):
    """Fewer rows than the cap: everything comes back, nothing is flagged."""
    slugs = await _seed(test_session, 3)

    result = await fetch_bounded(
        test_session,
        _stmt(),
        cap=10,
        order_by=ArchitectureRelationship.slug,
        label=_LABEL,
    )

    assert isinstance(result, BoundedRows)
    assert result.truncated is False
    assert result.returned == 3
    assert result.total == 3
    # The right rows, not merely the right count.
    assert [r.slug for r in result.rows] == slugs


async def test_exactly_at_cap_is_not_truncated(test_session: AsyncSession):
    """Exactly ``cap`` rows is a complete answer, not a truncated one.

    This is the classic off-by-one: an implementation that flags truncation on
    ``len(rows) >= cap`` (rather than ``> cap``) refuses a request it could have
    served, and the ``cap + 1`` probe row is what makes the distinction real.
    """
    slugs = await _seed(test_session, 5)

    result = await fetch_bounded(
        test_session,
        _stmt(),
        cap=5,
        order_by=ArchitectureRelationship.slug,
        label=_LABEL,
    )

    assert result.truncated is False
    assert result.returned == 5
    assert result.total == 5
    assert [r.slug for r in result.rows] == slugs


async def test_over_cap_truncates_and_reports_the_true_total(test_session: AsyncSession):
    """Over the cap: exactly ``cap`` rows, and ``total`` is the FULL count.

    ``total`` must be 7 here — not 3 (the cap), and not 4 (the ``cap + 1`` rows
    that were actually fetched). This is the assertion that dies if the second
    COUNT query is missing, wrong, or accidentally inherits the ``LIMIT``.
    """
    slugs = await _seed(test_session, 7)

    result = await fetch_bounded(
        test_session,
        _stmt(),
        cap=3,
        order_by=ArchitectureRelationship.slug,
        label=_LABEL,
    )

    assert result.truncated is True
    assert len(result.rows) == 3
    assert result.returned == 3
    assert result.total == 7
    # The probe row is dropped, not handed back: the prefix is the ORDERED one.
    assert [r.slug for r in result.rows] == slugs[:3]


async def test_raise_if_truncated_raises_when_truncated(test_session: AsyncSession):
    """A truncated result refuses on demand rather than silently analysing a prefix."""
    await _seed(test_session, 7)

    result = await fetch_bounded(
        test_session,
        _stmt(),
        cap=3,
        order_by=ArchitectureRelationship.slug,
        label=_LABEL,
    )

    with pytest.raises(InputTooLarge):
        result.raise_if_truncated()


async def test_raise_if_truncated_is_silent_when_not_truncated(test_session: AsyncSession):
    """A complete result must pass through untouched — the guard is not a tollgate."""
    await _seed(test_session, 3)

    result = await fetch_bounded(
        test_session,
        _stmt(),
        cap=10,
        order_by=ArchitectureRelationship.slug,
        label=_LABEL,
    )

    result.raise_if_truncated()  # must not raise


async def test_exception_carries_the_numbers_an_operator_needs(test_session: AsyncSession):
    """``InputTooLarge`` names the label, the true total and the limit — in its ``str``.

    An operator reading a 4xx body has to be able to tell how far over the bound
    they are without opening a shell. A message that says only "too large" makes
    "narrow the request" and "raise the limit" indistinguishable choices.
    """
    await _seed(test_session, 7)

    result = await fetch_bounded(
        test_session,
        _stmt(),
        cap=3,
        order_by=ArchitectureRelationship.slug,
        label=_LABEL,
    )

    with pytest.raises(InputTooLarge) as excinfo:
        result.raise_if_truncated()

    exc = excinfo.value
    assert exc.total == 7
    assert exc.limit == 3
    assert exc.label == _LABEL
    rendered = str(exc)
    assert "7" in rendered
    assert "3" in rendered
    assert _LABEL in rendered


async def test_columns_only_select_returns_row_tuples(test_session: AsyncSession):
    """A columns-only SELECT is consumed as rows, not scalars.

    ``fetch_bounded`` has to serve both shapes: the loaders it was written for
    select whole entities, but the house pattern for an edge load is a
    columns-only select consumed with ``.all()``.
    Running that through ``.scalars()`` would silently drop the second column
    and hand the caller a list of source ids that still passes ``len()``.
    """
    await _seed(test_session, 3)

    stmt = select(ArchitectureRelationship.slug, ArchitectureRelationship.relationship_type).where(
        ArchitectureRelationship.tenant_id == _TENANT
    )
    result = await fetch_bounded(
        test_session,
        stmt,
        cap=10,
        order_by=ArchitectureRelationship.slug,
        label=_LABEL,
    )

    assert result.truncated is False
    assert result.total == 3
    assert [tuple(row) for row in result.rows] == [
        ("bounded-rel-0000", "association"),
        ("bounded-rel-0001", "association"),
        ("bounded-rel-0002", "association"),
    ]


async def test_the_bound_is_in_the_SQL_not_in_python(test_session: AsyncSession):
    """The ``LIMIT`` must reach Postgres — slicing in Python is not a bound.

    ⛔ THIS TEST EXISTS BECAUSE EVERY OTHER TEST IN THIS FILE SURVIVED ITS
    REMOVAL. Deleting ``.limit(cap + 1)`` from ``fetch_bounded`` changes NOTHING
    observable: the helper still fetches every row, still slices to ``cap``,
    still reports the same ``total`` and the same ``truncated``. Measured — the
    mutation was applied and all 7 tests stayed green.

    That is the entire defect this module was written to prevent. The rows would
    be resident in the heap before anything sliced them, which is precisely how
    the unbounded loaders OOM-killed this pod: the cost is paid at fetch time,
    not at return time. A bound that is only enforced after the rows arrive is
    decoration.

    So this test asserts the emitted SQL, which is the only place the property
    is actually visible.
    """
    from sqlalchemy import event

    await _seed(test_session, 7)

    statements: list[str] = []
    # ``AsyncSession.get_bind()`` hands back the SYNC ``Engine`` (verified), so
    # the listener attaches to it directly — there is no ``.sync_engine`` here.
    engine = test_session.get_bind()

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        result = await fetch_bounded(
            test_session,
            _stmt(),
            cap=3,
            order_by=ArchitectureRelationship.slug,
            label=_LABEL,
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    # Control: the call really did go over the bound, so a LIMIT was warranted.
    assert result.truncated is True

    selects = [s for s in statements if "FROM architecture_relationships" in s]
    assert selects, f"no SELECT was captured; the listener is blind: {statements}"

    # The row-fetching SELECT — not the COUNT — must carry a LIMIT.
    row_selects = [s for s in selects if "count(" not in s.lower()]
    assert row_selects, f"no row SELECT captured, only counts: {selects}"
    assert all("LIMIT" in s.upper() for s in row_selects), (
        f"the row SELECT reached Postgres with no LIMIT: {row_selects}"
    )

"""Every route on the wrapped router still answers normally — and one of them
had no test at all.

`architecture_elements` gained ``route_class=BoundedInputRoute`` so that
``InputTooLarge`` from ``batch-delete``'s bounded filter maps to 422 instead of
escaping as a 500. A ``route_class`` is per-ROUTER, so it wraps all sixteen
operations, not just the one that needed it. That is the blast radius, and the
first evidence offered for it was measured and found wanting: of the sixteen
wrapped routes, five were never requested by any suite cited as proof.

Four of the five are covered by suites that simply were not run
(``test_arch_hierarchy``, ``test_children_is_bounded``, ``test_bola_security``,
``test_quality_persistence``). The fifth — ``compare`` — had no test anywhere:
``091f684:tests/test_coverage_boost.py`` compared versions for ``/principles`` and ``/decisions``
but never for architecture elements, which is why it sat on the
route-coverage grandfathered list. This file closes it.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

_ELEMENT = {
    "description": "version-compare subject",
    "layer": "application",
    "element_type": "application_component",
    "status": "draft",
    "architecture_state": "baseline",
}


@pytest.mark.asyncio
async def test_architecture_element_version_compare(authenticated_client: AsyncClient) -> None:
    """GET /architecture/elements/{eid}/versions/{v1}/compare/{v2} compares snapshots."""
    created = await authenticated_client.post(
        "/architecture/elements",
        json={**_ELEMENT, "title": "Compare E", "version": "1.0.0"},
    )
    assert created.status_code == 200, created.text
    eid = created.json()["id"]

    updated = await authenticated_client.put(
        f"/architecture/elements/{eid}",
        json={**_ELEMENT, "title": "Compare E Updated", "version": "2.0.0"},
    )
    assert updated.status_code == 200, updated.text

    resp = await authenticated_client.get(
        f"/architecture/elements/{eid}/versions/1.0.0/compare/2.0.0"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["old_version"] == "1.0.0"
    assert data["new_version"] == "2.0.0"


@pytest.mark.asyncio
async def test_an_unknown_version_is_a_clean_refusal_not_a_500(
    authenticated_client: AsyncClient,
) -> None:
    """The route_class must pass non-``InputTooLarge`` errors through untouched.

    A 500 here would mean the wrapper swallowed or re-cast an ordinary failure —
    exactly the regression a router-wide hook can introduce silently.
    """
    created = await authenticated_client.post(
        "/architecture/elements",
        json={**_ELEMENT, "title": "Compare E missing", "version": "1.0.0"},
    )
    assert created.status_code == 200, created.text
    eid = created.json()["id"]

    resp = await authenticated_client.get(
        f"/architecture/elements/{eid}/versions/9.0.0/compare/10.0.0"
    )
    assert resp.status_code != 500, resp.text
    assert resp.status_code in (404, 422), resp.text

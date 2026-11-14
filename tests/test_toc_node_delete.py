"""``DELETE /toc/{tree_id}/nodes/{node_id}`` — the only destructive, cascading-
looking TOC operation with no prior coverage.

Read from ``routers.toc.delete_toc_node`` (``src/routers/toc.py``) and
``services.toc_service.cleanup_node_edges``:

    node = await node_svc._get_mutable(session, node_id, tenant_id=tenant_id, subject_id=subject_id)
    if node.tree_id != tree_id:
        raise HTTPException(status_code=404, detail="not found")
    await toc_service.cleanup_node_edges(session, node_id, tenant_id)
    await session.delete(node)
    await session.commit()

``TocNode`` (``models/toc.py``) carries no ``parent_id`` column at all — the
only structure a TOC tree has is the source/target edge, stored as
``architecture_relationships`` rows scoped to ``toc_nodes``. So there is no
parent/child column to cascade or reparent. The handler's real behaviour on a
non-leaf node (one with edges pointing INTO it and/or OUT of it) is:

* the node's OWN edges (either endpoint) are deleted via ``cleanup_node_edges``;
* the node row itself is deleted;
* every OTHER node those edges touched is left completely alone — not
  deleted, not reparented, not modified — now simply missing the edge that
  used to connect it to the deleted node.

That is neither a cascade-delete of descendants nor a reparent; it is closer
to "delete the node and orphan its neighbours". These tests pin that
behaviour so a future change that starts silently cascading (or silently
leaves a dangling edge) goes red.

Cross-tenant access reuses ``EntityService._get_mutable`` /
``_get_visible``, which scope the lookup by ``tenant_id`` and return a plain
404 (not 403) when the row is not visible — the module's own docstring calls
this out as "matches the secrets/BOLA pattern: don't leak the existence of a
sibling user's private row." The tenant-isolation test below pins that real
status code rather than assuming 403.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import func, select

from models.architecture import ArchitectureRelationship
from models.toc import TocNode
from tests.tenants import T

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_toc.py / tests/test_toc_edge_scoping.py style)
# ---------------------------------------------------------------------------


async def _create_tree(client: AsyncClient, tree_type: str = "io_map", title: str = "T") -> dict:
    resp = await client.post("/toc", json={"title": title, "tree_type": tree_type})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add_node(client: AsyncClient, tree_id: str, kind: str, title: str) -> dict:
    resp = await client.post(
        f"/toc/{tree_id}/nodes",
        json={"title": title, "statement": f"{title} statement", "node_kind": kind},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add_edge(client: AsyncClient, tree_id: str, src: str, tgt: str, rel: str) -> dict:
    resp = await client.post(
        f"/toc/{tree_id}/edges",
        json={"source_id": src, "target_id": tgt, "relationship_type": rel},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _node_row_count(session_factory, node_id: str) -> int:
    """Count the node row through an INDEPENDENT session (real read-back)."""
    async with session_factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(TocNode).where(TocNode.id == uuid.UUID(node_id))
            )
        ).scalar_one()


async def _edge_row_count_touching(session_factory, node_id: str) -> int:
    """Count architecture_relationships rows (toc_nodes side) with either endpoint == node_id."""
    async with session_factory() as session:
        node_uuid = uuid.UUID(node_id)
        return (
            await session.execute(
                select(func.count())
                .select_from(ArchitectureRelationship)
                .where(
                    ArchitectureRelationship.source_entity_type == "toc_nodes",
                    (ArchitectureRelationship.source_id == node_uuid)
                    | (ArchitectureRelationship.target_id == node_uuid),
                )
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Non-leaf delete: edges cleaned up, node gone, neighbours left in place
# ---------------------------------------------------------------------------


class TestDeleteNonLeafNode:
    async def test_delete_non_leaf_node_clears_its_edges_but_leaves_the_other_nodes(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """A 3-node IO-Map chain: nc --necessity--> csf --necessity--> goal.

        ``csf`` is non-leaf: it has one edge INTO it (from ``nc``) and one
        edge OUT of it (to ``goal``) — a genuine causal link on both sides,
        matching production's longest chain (2 edges). Deleting ``csf`` is
        the case under test.

        KILLING MUTATION: dropping the ``cleanup_node_edges`` call would leave
        both edges pointing at a now-nonexistent node id, and the edge-count
        assertion below would go red (rowcount stays 2, not 0). A change that
        started cascading (deleting ``goal``/``nc`` too) would make the
        surviving-node-ids assertion go red instead.
        """
        tree = await _create_tree(authenticated_client, "io_map", "delete-csf")
        goal = await _add_node(authenticated_client, tree["id"], "goal", "Ship the product on time")
        csf = await _add_node(
            authenticated_client, tree["id"], "csf", "Engineering capacity is sufficient"
        )
        nc = await _add_node(
            authenticated_client, tree["id"], "necessary_condition", "Hire two engineers"
        )
        await _add_edge(authenticated_client, tree["id"], csf["id"], goal["id"], "toc_necessity")
        await _add_edge(authenticated_client, tree["id"], nc["id"], csf["id"], "toc_necessity")

        assert await _node_row_count(test_session_factory, csf["id"]) == 1
        assert await _edge_row_count_touching(test_session_factory, csf["id"]) == 2

        resp = await authenticated_client.delete(f"/toc/{tree['id']}/nodes/{csf['id']}")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "deleted", "id": csf["id"]}

        # The node itself is gone.
        assert await _node_row_count(test_session_factory, csf["id"]) == 0

        # Every edge that referenced it (either endpoint) is cleaned up too.
        assert await _edge_row_count_touching(test_session_factory, csf["id"]) == 0

        # No cascade, no reparent: the neighbouring nodes survive untouched,
        # now simply disconnected from the deleted node.
        remaining = await authenticated_client.get(f"/toc/{tree['id']}/nodes")
        assert remaining.status_code == 200
        remaining_ids = {n["id"] for n in remaining.json()}
        assert remaining_ids == {goal["id"], nc["id"]}

        # Both edges touched the deleted node, so none survive.
        edges_resp = await authenticated_client.get(f"/toc/{tree['id']}/edges")
        assert edges_resp.json() == []


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestDeleteNodeTenantIsolation:
    async def test_cross_tenant_delete_is_404_and_the_node_survives(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """Another tenant's node is not deletable through this URL.

        ``_get_mutable``/``_get_visible`` scope the lookup by ``tenant_id``,
        so the row is simply not visible to the other tenant and the handler
        404s before it ever reaches ``cleanup_node_edges`` — not 403, per the
        service's own documented BOLA posture.
        """
        tree = await _create_tree(authenticated_client, "crt", "owner-tree")
        node = await _add_node(authenticated_client, tree["id"], "ude", "Owned by tenant A")
        assert await _node_row_count(test_session_factory, node["id"]) == 1

        other_tenant = T("toc-delete-other-tenant")
        resp = await authenticated_client.delete(
            f"/toc/{tree['id']}/nodes/{node['id']}",
            headers={"X-Tenant-Id": other_tenant},
        )

        assert resp.status_code == 404, resp.text
        assert await _node_row_count(test_session_factory, node["id"]) == 1

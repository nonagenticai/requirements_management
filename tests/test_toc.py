"""Logical Thinking Process (/toc) — CRUD, graph, CLR scrutiny, config.

API tests hit real PostgreSQL through the FastAPI test client (no mocks).
The CLR scorer is also unit-tested directly with lightweight stand-ins.
"""

import uuid
from types import SimpleNamespace

from httpx import AsyncClient

from services.toc_service import score_tree_clr
from tests.tenants import T

# asyncio_mode="auto" (pyproject) auto-detects async tests; no pytestmark needed,
# and omitting it keeps the sync CLR unit tests below from being marked async.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_tree(client: AsyncClient, tree_type: str = "io_map", **over) -> dict:
    payload = {"title": f"Tree {tree_type}", "tree_type": tree_type, **over}
    resp = await client.post("/toc", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add_node(client: AsyncClient, tree_id: str, kind: str, statement: str) -> dict:
    resp = await client.post(
        f"/toc/{tree_id}/nodes",
        json={
            "tree_id": tree_id,
            "title": statement[:40],
            "statement": statement,
            "node_kind": kind,
        },
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


# ---------------------------------------------------------------------------
# Tree / node / edge / graph
# ---------------------------------------------------------------------------


class TestTocCrud:
    async def test_create_tree(self, authenticated_client: AsyncClient):
        data = await _create_tree(authenticated_client, "io_map")
        assert data["tree_type"] == "io_map"
        assert data["tenant_id"] == T("user-123")
        assert data["slug"] and data["id"]

    async def test_invalid_tree_type_422(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.post("/toc", json={"title": "x", "tree_type": "nope"})
        assert resp.status_code == 422

    async def test_node_kind_validated_against_tree_type(self, authenticated_client: AsyncClient):
        tree = await _create_tree(authenticated_client, "io_map")
        # 'ude' belongs to a CRT, not an IO-Map.
        resp = await authenticated_client.post(
            f"/toc/{tree['id']}/nodes",
            json={
                "tree_id": tree["id"],
                "title": "x",
                "statement": "an undesirable effect",
                "node_kind": "ude",
            },
        )
        assert resp.status_code == 422
        assert "invalid for io_map" in resp.text

    async def test_evaporating_cloud_accepts_assumption_and_injection(
        self, authenticated_client: AsyncClient
    ):
        # 2.3: the auto-EC builder persists assumption + injection nodes (+ a
        # toc_assumption edge onto the conflict) in addition to the 5 boxes.
        tree = await _create_tree(authenticated_client, "evaporating_cloud")
        d = await _add_node(authenticated_client, tree["id"], "prerequisite", "Ship this week")
        assumption = await _add_node(
            authenticated_client, tree["id"], "assumption", "Speed requires skipping tests"
        )
        await _add_node(authenticated_client, tree["id"], "injection", "Parallelize the test suite")
        await _add_edge(
            authenticated_client, tree["id"], assumption["id"], d["id"], "toc_assumption"
        )

        graph = (await authenticated_client.get(f"/toc/{tree['id']}/graph")).json()
        kinds = {n["node_kind"] for n in graph["nodes"]}
        assert {"assumption", "injection"} <= kinds
        assert {"toc_assumption"} <= {e["relationship_type"] for e in graph["edges"]}

    async def test_full_graph_roundtrip(self, authenticated_client: AsyncClient):
        tree = await _create_tree(authenticated_client, "io_map")
        goal = await _add_node(authenticated_client, tree["id"], "goal", "Ship the product on time")
        csf = await _add_node(
            authenticated_client, tree["id"], "csf", "Engineering capacity is sufficient"
        )
        nc = await _add_node(
            authenticated_client, tree["id"], "necessary_condition", "Hire two engineers"
        )
        await _add_edge(authenticated_client, tree["id"], csf["id"], goal["id"], "toc_necessity")
        await _add_edge(authenticated_client, tree["id"], nc["id"], csf["id"], "toc_necessity")

        resp = await authenticated_client.get(f"/toc/{tree['id']}/graph")
        assert resp.status_code == 200
        graph = resp.json()
        assert graph["tree"]["id"] == tree["id"]
        assert len(graph["nodes"]) == 3
        assert len(graph["edges"]) == 2
        assert {e["relationship_type"] for e in graph["edges"]} == {"toc_necessity"}

    async def test_edge_rejects_node_from_other_tree(self, authenticated_client: AsyncClient):
        tree_a = await _create_tree(authenticated_client, "io_map")
        tree_b = await _create_tree(authenticated_client, "io_map")
        a_goal = await _add_node(authenticated_client, tree_a["id"], "goal", "Goal of tree A")
        b_goal = await _add_node(authenticated_client, tree_b["id"], "goal", "Goal of tree B")
        resp = await authenticated_client.post(
            f"/toc/{tree_a['id']}/edges",
            json={
                "source_id": b_goal["id"],
                "target_id": a_goal["id"],
                "relationship_type": "toc_necessity",
            },
        )
        assert resp.status_code == 404

    async def test_delete_tree_cascades_nodes_and_edges(self, authenticated_client: AsyncClient):
        tree = await _create_tree(authenticated_client, "crt")
        ude = await _add_node(
            authenticated_client, tree["id"], "ude", "Customers churn after onboarding"
        )
        root = await _add_node(
            authenticated_client, tree["id"], "root_cause", "Onboarding has no guidance"
        )
        await _add_edge(authenticated_client, tree["id"], root["id"], ude["id"], "toc_sufficiency")

        resp = await authenticated_client.delete(f"/toc/{tree['id']}")
        assert resp.status_code == 200
        assert resp.json()["nodes_deleted"] == 2
        assert (await authenticated_client.get(f"/toc/{tree['id']}")).status_code == 404
        # Edges between the (now deleted) nodes are gone from the universal table.
        rels = await authenticated_client.get(
            "/architecture/relationships",
            params={"source_entity_type": "toc_nodes", "source_id": root["id"]},
        )
        assert rels.status_code == 200
        assert rels.json() == []


# ---------------------------------------------------------------------------
# CLR quality endpoint
# ---------------------------------------------------------------------------


class TestTocQuality:
    async def test_quality_endpoint_scores_and_persists(self, authenticated_client: AsyncClient):
        tree = await _create_tree(authenticated_client, "crt")
        ude = await _add_node(
            authenticated_client, tree["id"], "ude", "Releases slip every single sprint"
        )
        cause = await _add_node(
            authenticated_client, tree["id"], "root_cause", "Requirements change mid sprint"
        )
        await _add_edge(authenticated_client, tree["id"], cause["id"], ude["id"], "toc_sufficiency")

        resp = await authenticated_client.post(f"/toc/{tree['id']}/quality")
        assert resp.status_code == 200
        report = resp.json()
        assert 0.0 <= report["score"] <= 1.0
        assert len(report["categories"]) == 8
        # Score is persisted back onto the tree.
        tree_after = (await authenticated_client.get(f"/toc/{tree['id']}")).json()
        assert tree_after["quality_score"] == report["score"]
        assert tree_after["last_evaluated_by_agent"] == "clr_structural"


# ---------------------------------------------------------------------------
# Config (global -> tenant -> project)
# ---------------------------------------------------------------------------


class TestTocConfig:
    async def test_default_then_tenant_override(self, authenticated_client: AsyncClient):
        default = (await authenticated_client.get("/toc/config")).json()
        assert default["settings"]["ec_auto_severity_threshold"] == "medium"

        put = await authenticated_client.put(
            "/toc/config", json={"settings": {"ec_auto_severity_threshold": "high"}}
        )
        assert put.status_code == 200
        assert put.json()["settings"]["ec_auto_severity_threshold"] == "high"
        # enabled_lenses still falls through from the hardcoded default.
        assert "enabled_lenses" in put.json()["settings"]

        again = (await authenticated_client.get("/toc/config")).json()
        assert again["settings"]["ec_auto_severity_threshold"] == "high"


# ---------------------------------------------------------------------------
# CLR scorer — direct unit tests with lightweight stand-ins
# ---------------------------------------------------------------------------


def _node(statement: str):
    return SimpleNamespace(id=uuid.uuid4(), statement=statement)


def _edge(src, tgt, rel):
    return SimpleNamespace(source_id=src.id, target_id=tgt.id, relationship_type=rel)


class TestClrScorer:
    def test_empty_tree_scores_zero(self):
        score, _passes, issues, categories = score_tree_clr("crt", [], [])
        assert score == 0.0
        assert all(v is False for v in categories.values())
        assert any("no nodes" in i.lower() for i in issues)

    def test_sound_sufficiency_tree_scores_high(self):
        cause = _node("Requirements change mid sprint")
        effect = _node("Releases slip every sprint badly")
        edges = [_edge(cause, effect, "toc_sufficiency")]
        score, _p, _i, categories = score_tree_clr("crt", [cause, effect], edges)
        assert categories["causality_existence"] is True
        assert categories["cause_effect_reversal"] is True  # acyclic
        assert categories["predicted_effect_existence"] is True  # effect is a sink
        assert categories["tautology"] is True
        assert score >= 0.75

    def test_cycle_flags_cause_effect_reversal(self):
        a = _node("Alpha causes beta strongly here")
        b = _node("Beta causes alpha strongly here")
        edges = [_edge(a, b, "toc_sufficiency"), _edge(b, a, "toc_sufficiency")]
        _s, _p, _i, categories = score_tree_clr("crt", [a, b], edges)
        assert categories["cause_effect_reversal"] is False

    def test_duplicate_statements_flag_tautology(self):
        a = _node("The same statement repeated here")
        b = _node("The same statement repeated here")
        edges = [_edge(a, b, "toc_sufficiency")]
        _s, _p, _i, categories = score_tree_clr("crt", [a, b], edges)
        assert categories["tautology"] is False

    def test_orphan_node_flags_cause_insufficiency(self):
        cause = _node("A genuine cause statement here")
        effect = _node("A resulting effect statement here")
        orphan = _node("An unconnected floating statement")
        edges = [_edge(cause, effect, "toc_sufficiency")]
        _s, _p, _i, categories = score_tree_clr("crt", [cause, effect, orphan], edges)
        assert categories["cause_insufficiency"] is False


class TestTocEdgeIdempotency:
    """FIX 6 (WS1 hardening): create_toc_edge is idempotent on re-POST.

    TOC edges carry no project_id; before the fix an identical re-POST silently
    duplicated (the WS1 edge index is partial on project_id IS NOT NULL). Now
    the uq_arch_rels_toc_edge partial index backstops get_or_create.
    """

    async def test_edge_repost_is_idempotent(self, authenticated_client: AsyncClient):
        tree = await _create_tree(authenticated_client, "io_map")
        goal = await _add_node(authenticated_client, tree["id"], "goal", "Ship on time")
        csf = await _add_node(authenticated_client, tree["id"], "csf", "Have a working build")

        e1 = await _add_edge(
            authenticated_client, tree["id"], goal["id"], csf["id"], "toc_necessity"
        )
        e2 = await _add_edge(
            authenticated_client, tree["id"], goal["id"], csf["id"], "toc_necessity"
        )
        assert e2["id"] == e1["id"], "identical toc edge re-POST must return the same edge id"

        graph = (await authenticated_client.get(f"/toc/{tree['id']}/graph")).json()
        matching = [
            edge
            for edge in graph["edges"]
            if edge["source_id"] == goal["id"]
            and edge["target_id"] == csf["id"]
            and edge["relationship_type"] == "toc_necessity"
        ]
        assert len(matching) == 1, "re-POST must not create a duplicate edge row"

    async def test_different_relationship_type_creates_second_edge(
        self, authenticated_client: AsyncClient
    ):
        tree = await _create_tree(authenticated_client, "io_map")
        a = await _add_node(authenticated_client, tree["id"], "goal", "Goal node")
        b = await _add_node(authenticated_client, tree["id"], "csf", "CSF node")

        e1 = await _add_edge(authenticated_client, tree["id"], a["id"], b["id"], "toc_necessity")
        e2 = await _add_edge(authenticated_client, tree["id"], a["id"], b["id"], "toc_sufficiency")
        assert e2["id"] != e1["id"], "a different relationship_type is a distinct edge"

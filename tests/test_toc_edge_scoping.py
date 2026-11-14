"""TOC edge tree-scoping, the path-owned ``tree_id``, and the request vocabularies.

Three defects, one file:

**T1 — ``DELETE /toc/{tree_id}/edges/{edge_id}`` ignored ``tree_id`` entirely.**
The route handed ``edge_id`` straight to the GENERIC relationship service over
``architecture_relationships`` — the same table that carries ArchiMate
relationships and governance ``traceability`` links — so ANY relationship row
in the tenant was deletable through a TOC URL carrying an arbitrary tree UUID.
Every sibling route calls ``_require_tree`` first; this one did not, and the
declared ``tree_id`` path parameter was never read.

**T1c — and the first fix for T1 left one axis open.** Checking the two
``*_entity_type``s and both endpoints' tree membership still let through an edge
whose ``relationship_type`` is outside ``TOC_EDGE_TYPES`` — e.g. an
``association`` between two nodes of the same tree. ``create_toc_edge`` cannot
mint such a row (``TocEdgeIn.relationship_type`` is the ``TocEdgeType``
Literal), so it can only have come from ``POST /architecture/relationships``,
and the TOC path was still a delete primitive for a row it cannot create.

**T2 — ``TocNodeIn.tree_id`` was required in the body** while its own docstring
said the router overwrites it from the path. The override happens *after*
validation, so a tester posting the documented body got 422 on the first call.

**T3 — the toc vocabularies were invisible to OpenAPI.** ``tree_type``,
``node_kind`` and ``relationship_type`` were plain ``str`` policed by
``field_validator``s. A validator returns a correct 422 but emits no ``enum``,
so every generated client rendered a free-text box.

The API tests run against real PostgreSQL through the FastAPI test client (no
mocks) and read the row back through an independent session, because a route
that returns 404 while still deleting the row would pass a status-code-only
assertion.
"""

import uuid
from typing import get_args

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, update

from models.architecture import ArchitectureRelationship
from models.toc import ALL_NODE_KINDS, TREE_TYPES
from schemas.toc import (
    TOC_EDGE_TYPES,
    TocEdgeIn,
    TocEdgeType,
    TocNodeIn,
    TocNodeKind,
    TocTreeIn,
    TocTreeType,
)
from tests.tenants import T

# asyncio_mode="auto" (pyproject) auto-detects the async tests; the pure-schema
# vocabulary tests below are sync on purpose and must stay unmarked.

TENANT = T("user-123")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_tree(client: AsyncClient, tree_type: str = "crt", title: str = "T") -> dict:
    resp = await client.post("/toc", json={"title": title, "tree_type": tree_type})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add_node(client: AsyncClient, tree_id: str, kind: str, title: str) -> dict:
    resp = await client.post(
        f"/toc/{tree_id}/nodes",
        json={
            "tree_id": tree_id,
            "title": title,
            "statement": f"{title} statement",
            "node_kind": kind,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add_edge(client: AsyncClient, tree_id: str, src: str, tgt: str) -> dict:
    resp = await client.post(
        f"/toc/{tree_id}/edges",
        json={"source_id": src, "target_id": tgt, "relationship_type": "toc_sufficiency"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _crt_edge(client: AsyncClient, title_prefix: str) -> tuple[dict, dict]:
    """A CRT tree plus one ``toc_sufficiency`` edge between two of its nodes."""
    tree = await _create_tree(client, "crt", f"{title_prefix} tree")
    cause = await _add_node(client, tree["id"], "root_cause", f"{title_prefix} cause")
    effect = await _add_node(client, tree["id"], "ude", f"{title_prefix} effect")
    edge = await _add_edge(client, tree["id"], cause["id"], effect["id"])
    return tree, edge


async def _row_count(session_factory, rel_id: str) -> int:
    """Count the relationship row through an INDEPENDENT session.

    A fresh session (rather than one held open across the request) is what makes
    this a real read-back: the router commits, and READ COMMITTED then shows the
    committed state instead of a snapshot taken before the call.
    """
    async with session_factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(ArchitectureRelationship)
                .where(ArchitectureRelationship.id == uuid.UUID(rel_id))
            )
        ).scalar_one()


async def _retype_edge(session_factory, rel_id: str, relationship_type: str) -> None:
    """Rewrite one row's ``relationship_type`` in place, through a direct UPDATE.

    Deliberately NOT a fresh INSERT. The row is first created by the real
    ``POST /toc/{tree}/edges`` route, so it carries the caller's own
    ``tenant_id`` / ``subject_id`` / ``visibility`` / slug — which is what makes
    ``_get_mutable`` REACH it. The single UPDATE then leaves
    ``relationship_type`` as the ONE difference from the control edge, so a 404
    can only come from the guard under test and never from the row being
    invisible. ``create_toc_edge`` cannot produce this state (its ``TocEdgeType``
    Literal rejects anything outside ``TOC_EDGE_TYPES``); the generic
    ``POST /architecture/relationships`` can, and this is that row's shape.
    """
    async with session_factory() as session:
        await session.execute(
            update(ArchitectureRelationship)
            .where(ArchitectureRelationship.id == uuid.UUID(rel_id))
            .values(relationship_type=relationship_type)
        )
        await session.commit()


async def _edge_type(session_factory, rel_id: str) -> str | None:
    """Read the stored ``relationship_type`` back through an independent session."""
    async with session_factory() as session:
        return (
            await session.execute(
                select(ArchitectureRelationship.relationship_type).where(
                    ArchitectureRelationship.id == uuid.UUID(rel_id)
                )
            )
        ).scalar_one_or_none()


async def _seed_governance_link(session_factory) -> str:
    """Insert a NON-toc relationship: a requirement ``traceability`` link.

    Inserted directly rather than through the API so the row is unambiguously
    outside every TOC tree while sharing the tenant and ``visibility='tenant'``
    — i.e. exactly the row the pre-fix route would happily delete.
    """
    rel_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            ArchitectureRelationship(
                id=rel_id,
                slug=f"gov-link-{rel_id}",
                tenant_id=TENANT,
                tenant_name="user-123",
                subject_id="gov-owner",
                visibility="tenant",
                project_id="proj-1",
                source_entity_type="requirements",
                source_id=uuid.uuid4(),
                target_entity_type="requirements",
                target_id=uuid.uuid4(),
                relationship_type="traceability",
                name="A traces to B",
                properties={},
            )
        )
        await session.commit()
    return str(rel_id)


# ---------------------------------------------------------------------------
# T1 — the delete route must be scoped to the tree in the path
# ---------------------------------------------------------------------------


class TestTocEdgeDeleteIsTreeScoped:
    async def test_control_edge_of_this_tree_is_deleted(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """CONTROL — a legitimate delete still succeeds and the row IS gone.

        Without this leg, "404 on everything" would pass the cross-tree test
        vacuously. This must pass both before and after the fix.
        """
        tree, edge = await _crt_edge(authenticated_client, "control")
        assert await _row_count(test_session_factory, edge["id"]) == 1

        resp = await authenticated_client.delete(f"/toc/{tree['id']}/edges/{edge['id']}")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "deleted"
        assert await _row_count(test_session_factory, edge["id"]) == 0

    async def test_edge_of_another_tree_is_404_and_survives(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """An edge of tree B is not deletable through tree A's URL.

        KILLING MUTATION: ``drop-tree-check`` — reverting ``delete_toc_edge`` to
        ``return await edge_svc.delete(session, edge_id, tenant_id, subject_id)``
        (no ``_require_tree``, no endpoint check) turns this RED: the response
        becomes 200 and the row count drops to 0.

        404, not 403: a 403 would confirm to the caller that the row exists.
        """
        tree_a = await _create_tree(authenticated_client, "crt", "tree A")
        _tree_b, edge_b = await _crt_edge(authenticated_client, "tree B")
        assert await _row_count(test_session_factory, edge_b["id"]) == 1

        resp = await authenticated_client.delete(f"/toc/{tree_a['id']}/edges/{edge_b['id']}")

        assert resp.status_code == 404, resp.text
        assert await _row_count(test_session_factory, edge_b["id"]) == 1

    async def test_unknown_tree_id_is_404_and_the_edge_survives(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """An arbitrary (non-existent) tree UUID in the path deletes nothing."""
        _tree, edge = await _crt_edge(authenticated_client, "arbitrary")

        resp = await authenticated_client.delete(f"/toc/{uuid.uuid4()}/edges/{edge['id']}")

        assert resp.status_code == 404, resp.text
        assert await _row_count(test_session_factory, edge["id"]) == 1

    async def test_non_toc_relationship_is_not_deletable_through_the_toc_path(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """T1b — a governance ``traceability`` link survives a TOC delete.

        ``architecture_relationships`` is shared: before the fix this route was a
        tenant-wide relationship-delete primitive wearing a TOC URL.
        """
        tree = await _create_tree(authenticated_client, "crt", "unrelated tree")
        rel_id = await _seed_governance_link(test_session_factory)
        assert await _row_count(test_session_factory, rel_id) == 1

        resp = await authenticated_client.delete(f"/toc/{tree['id']}/edges/{rel_id}")

        assert resp.status_code == 404, resp.text
        assert await _row_count(test_session_factory, rel_id) == 1


# ---------------------------------------------------------------------------
# T1c — the delete route must also be scoped to the TOC edge vocabulary
# ---------------------------------------------------------------------------


class TestTocEdgeDeleteIsScopedToTheEdgeVocabulary:
    """The two legs are a matched pair: same tree, same endpoints, same row shape.

    The ONLY variable between them is ``relationship_type``. That is what makes
    the 404 leg non-vacuous — if the row were merely unreachable, the control
    below would 404 too.
    """

    async def test_control_a_real_toc_edge_of_this_tree_is_still_deleted(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """CONTROL — must pass BEFORE and AFTER the ``relationship_type`` check.

        Without it, a guard that 404s on everything would satisfy the leg below
        vacuously.
        """
        tree, edge = await _crt_edge(authenticated_client, "vocab control")
        assert await _edge_type(test_session_factory, edge["id"]) in TOC_EDGE_TYPES
        assert await _row_count(test_session_factory, edge["id"]) == 1

        resp = await authenticated_client.delete(f"/toc/{tree['id']}/edges/{edge['id']}")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "deleted"
        assert await _row_count(test_session_factory, edge["id"]) == 0

    async def test_non_toc_relationship_type_between_two_nodes_of_this_tree_is_404(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """An ``association`` between two nodes of tree A is NOT a TOC edge.

        ``create_toc_edge`` can only mint ``TOC_EDGE_TYPES`` — the request schema
        types the field as the ``TocEdgeType`` Literal — so this row came from
        ``POST /architecture/relationships``, and its own route family owns its
        delete. 404, not 403, exactly as the sibling guards: a 403 would confirm
        the row exists.

        KILLING MUTATION: ``drop-the-relationship-type-check`` — remove
        ``or edge.relationship_type not in TOC_EDGE_TYPES`` from
        ``delete_toc_edge``'s guard and this goes RED: the response becomes 200
        and the row count drops to 0.
        """
        tree, edge = await _crt_edge(authenticated_client, "vocab breach")
        await _retype_edge(test_session_factory, edge["id"], "association")
        assert await _edge_type(test_session_factory, edge["id"]) == "association"
        assert "association" not in TOC_EDGE_TYPES

        resp = await authenticated_client.delete(f"/toc/{tree['id']}/edges/{edge['id']}")

        assert resp.status_code == 404, resp.text
        assert await _row_count(test_session_factory, edge["id"]) == 1
        assert await _edge_type(test_session_factory, edge["id"]) == "association"


# ---------------------------------------------------------------------------
# T2 — tree_id belongs to the path, so the body must not require it
# ---------------------------------------------------------------------------


class TestNodeTreeIdComesFromThePath:
    async def test_body_without_tree_id_is_accepted(self, authenticated_client: AsyncClient):
        """The documented body (no ``tree_id``) must create, not 422."""
        tree = await _create_tree(authenticated_client, "crt", "path tree")

        resp = await authenticated_client.post(
            f"/toc/{tree['id']}/nodes",
            json={"title": "no tree_id", "statement": "body omits tree_id", "node_kind": "ude"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["tree_id"] == tree["id"]

    async def test_tree_id_in_the_body_is_overwritten_by_the_path(
        self, authenticated_client: AsyncClient
    ):
        """A foreign ``tree_id`` in the body cannot reparent the node."""
        tree = await _create_tree(authenticated_client, "crt", "owning tree")
        foreign = await _create_tree(authenticated_client, "crt", "foreign tree")

        resp = await authenticated_client.post(
            f"/toc/{tree['id']}/nodes",
            json={
                "tree_id": foreign["id"],
                "title": "reparent attempt",
                "statement": "body names another tree",
                "node_kind": "ude",
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["tree_id"] == tree["id"]

    def test_tree_id_is_optional_in_the_schema(self):
        """The field is not required, and the router's override still lands.

        ``uuid.UUID`` defines neither ``__bool__`` nor ``__len__``, so every UUID
        is truthy — the router's ``if payload.tree_id:`` get-or-create branch
        cannot flip to the plain-create branch once the path value is copied in.
        """
        assert "tree_id" not in TocNodeIn.model_json_schema().get("required", [])

        node = TocNodeIn(title="x", statement="y", node_kind="ude")
        assert node.tree_id is None

        path_tree_id = uuid.UUID(int=0)  # the nil UUID: the hardest truthiness case
        assert bool(path_tree_id) is True
        assert node.model_copy(update={"tree_id": path_tree_id}).tree_id == path_tree_id


# ---------------------------------------------------------------------------
# T3 — the vocabularies are Literal unions, so OpenAPI carries a real enum
# ---------------------------------------------------------------------------

# (alias, runtime constant, model, field name, a valid value, a mixed-case
#  spelling the pre-Literal validator accepted and coerced)
_VOCABULARIES = [
    pytest.param(TocTreeType, TREE_TYPES, TocTreeIn, "tree_type", "crt", "  CRT ", id="tree_type"),
    pytest.param(
        TocNodeKind, ALL_NODE_KINDS, TocNodeIn, "node_kind", "ude", " UDE ", id="node_kind"
    ),
    pytest.param(
        TocEdgeType,
        TOC_EDGE_TYPES,
        TocEdgeIn,
        "relationship_type",
        "toc_necessity",
        " TOC_Necessity ",
        id="relationship_type",
    ),
]

_REQUIRED_SIBLINGS: dict[type, dict] = {
    TocTreeIn: {"title": "vocab probe"},
    TocNodeIn: {"title": "vocab probe", "statement": "s"},
    TocEdgeIn: {
        "source_id": "0195f48b-d000-7000-8000-000000000001",
        "target_id": "0195f48b-d000-7000-8000-000000000002",
    },
}


def _build(model: type, field: str, value):
    return model(**{**_REQUIRED_SIBLINGS[model], field: value})


@pytest.mark.parametrize(("alias", "constant", "model", "field", "valid", "messy"), _VOCABULARIES)
class TestVocabulariesArePublishedEnums:
    def test_literal_matches_the_runtime_constant(
        self, alias, constant, model, field, valid, messy
    ):
        """KILLING MUTATION: ``drift-the-table``.

        Add, drop or rename a value in ``TREE_TYPES`` / ``ALL_NODE_KINDS`` /
        ``TOC_EDGE_TYPES`` — or in the matching ``Literal`` — and this goes RED.
        Reverting the field to a plain ``str`` also goes RED: ``get_args(str)``
        is ``()``.
        """
        assert set(get_args(alias)) == set(constant)
        assert get_args(alias), f"{field} is not a Literal union"
        # The alias must actually be what the FIELD is annotated with — without
        # this, reverting the field to a plain ``str`` while leaving the alias
        # defined would sail past the set-equality check above.
        assert model.model_fields[field].annotation is alias

    def test_openapi_property_carries_a_top_level_enum(
        self, alias, constant, model, field, valid, messy
    ):
        """The whole point of T3, and the guard against the ``| None`` trap.

        ``Literal[...] | None`` serialises as
        ``anyOf: [{enum: [...]}, {"type": "null"}]`` and the downstream n8n
        generator reads ``enum`` only at the TOP level of a property, so a
        nested enum is invisible — 13 production fields are already lost that
        way. A field_validator publishes no ``enum`` at all.
        """
        prop = model.model_json_schema()["properties"][field]

        assert "anyOf" not in prop, f"{field} enum is nested under anyOf — invisible to clients"
        assert set(prop["enum"]) == set(constant)

    def test_a_valid_value_is_accepted(self, alias, constant, model, field, valid, messy):
        assert getattr(_build(model, field, valid), field) == valid

    def test_every_member_of_the_constant_is_accepted(
        self, alias, constant, model, field, valid, messy
    ):
        """No member of the runtime vocabulary may be rejected by the Literal.

        ``node_kind`` is the one that matters: the router narrows it further
        against the tree's type, so the Literal must be the FULL cross-tree
        union or valid input breaks.
        """
        for member in sorted(constant):
            assert getattr(_build(model, field, member), field) == member

    def test_a_garbage_value_is_rejected(self, alias, constant, model, field, valid, messy):
        with pytest.raises(ValidationError):
            _build(model, field, "definitely-not-a-real-value")

    def test_normalisation_still_round_trips(self, alias, constant, model, field, valid, messy):
        """The replaced validators did ``.strip().lower()``.

        A bare ``Literal`` would newly reject ``" CRT "`` / ``" UDE "``, which
        the service accepts and coerces today; the ``mode="before"`` hook keeps
        that behaviour.
        """
        assert getattr(_build(model, field, messy), field) == messy.strip().lower()

    def test_non_string_input_is_a_validation_error_not_a_crash(
        self, alias, constant, model, field, valid, messy
    ):
        """The normaliser must pass non-``str`` through, not raise ``AttributeError``."""
        with pytest.raises(ValidationError):
            _build(model, field, 42)


def test_node_kind_literal_is_the_full_cross_tree_union():
    """Belt and braces on the one vocabulary assembled from six subsets."""
    assert len(get_args(TocNodeKind)) == len(ALL_NODE_KINDS) == 18
    assert set(get_args(TocNodeKind)) == set(ALL_NODE_KINDS)


def test_response_models_were_not_tightened():
    """``*Out`` models must stay ``str``.

    They carry no validator today, so a ``Literal`` there would be a NEW
    constraint on rows already in the database and one legacy value would 500
    the read path.
    """
    from schemas.toc import TocNodeOut, TocTreeOut

    assert TocTreeOut.model_fields["tree_type"].annotation is str
    assert TocNodeOut.model_fields["node_kind"].annotation is str
    for out_model, field in ((TocTreeOut, "tree_type"), (TocNodeOut, "node_kind")):
        prop = out_model.model_json_schema()["properties"][field]
        assert "enum" not in prop, f"{out_model.__name__}.{field} was tightened"

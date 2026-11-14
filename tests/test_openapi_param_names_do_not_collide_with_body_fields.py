"""A path/query param must not share its name with a body field it does not mean.

⛔ The defect this file exists to catch: ``GET /pm/projects/{eid}/brief/versions/{version}``
(``get_project_brief_version``) named its path param ``version``. The n8n node generator
(``n8n_module_scripts/scripts/generate-node.ts``) keys every field by its camelCased NAME
within one n8n resource (= the operation's first OpenAPI tag), and MERGES a path param with
any same-named request-body field of another operation in that resource into ONE n8n
property (default ``""``). So the brief's integer version and ``ProjectIn.version`` (the
project's semver string) became one parameter, and ``create_project`` / ``update_project``
started sending ``version: ""``. Fixed by renaming the path param to ``brief_version``.

Three checks, each over the whole of ``app.openapi()``:

1. **Generator merge rule** — a PATH param must not share its name with a requestBody
   property of ANY operation carrying the same first tag. This is the exact collision the
   generator makes. (Query params are emitted inside a separate ``additionalOptions``
   collection, so they do not merge.)
2. **Same operation** — no operation's path/query param may share a name with a property of
   that operation's own requestBody.
3. **PM projects** — no operation under ``/pm/projects`` may have a path/query param named
   like a ``ProjectIn`` property.

Pre-existing names that DO collide but mean the SAME value in both places (so the merge is
harmless) are listed below with the reason. An entry that no longer collides fails the test,
so the allowlist cannot rot into a blanket exemption.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from main import app

# (tag, param) — check 1. Same value in path and body, so one merged n8n field is correct.
SAME_MEANING_TAG_PATH_PARAMS: frozenset[tuple[str, str]] = frozenset(
    {
        # /toc/{tree_id}/... — the node body's tree_id IS the tree in the URL.
        ("Logical Thinking Process", "tree_id"),
        # /pm/projects/by-planka-board/{planka_board_id} — the link body's planka_board_id
        # (PUT /pm/projects/{eid}/planka-board) IS the same Planka board id.
        ("PM Projects", "planka_board_id"),
    }
)

# (operation_id, param) — check 2. Same value in the param and the body.
SAME_MEANING_SAME_OP_PARAMS: frozenset[tuple[str, str]] = frozenset(
    {
        ("create_toc_node", "tree_id"),
        ("update_toc_node", "tree_id"),
        # bulk deletes accept the confirmation flag as query OR body.
        ("bulk_delete_all", "confirm"),
        ("bulk_delete_decisions", "confirm"),
        ("bulk_delete_principles", "confirm"),
        ("bulk_delete_requirements", "confirm"),
    }
)

# (operation_id, param) — check 3. Query FILTERS on the field of the same name.
SAME_MEANING_PROJECT_PARAMS: frozenset[tuple[str, str]] = frozenset(
    {
        ("list_projects", "status"),
        ("list_projects", "programme_id"),
        ("list_projects", "origin_project_id"),
    }
)

_METHODS = ("get", "post", "put", "patch", "delete")


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return app.openapi()


def _props(
    spec: dict[str, Any], schema: dict[str, Any] | None, seen: tuple[str, ...] = ()
) -> set[str]:
    if not schema:
        return set()
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return set()
        return _props(spec, spec["components"]["schemas"][name], (*seen, name))
    out = set(schema.get("properties", {}))
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in schema.get(key, []):
            out |= _props(spec, sub, seen)
    return out


def _params(spec: dict[str, Any], op: dict[str, Any], where: tuple[str, ...]) -> set[str]:
    out = set()
    for p in op.get("parameters", []):
        if "$ref" in p:
            p = spec["components"]["parameters"][p["$ref"].rsplit("/", 1)[-1]]
        if p.get("in") in where:
            out.add(p["name"])
    return out


def _body_props(spec: dict[str, Any], op: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for media in op.get("requestBody", {}).get("content", {}).values():
        out |= _props(spec, media.get("schema"))
    return out


def _ops(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (path, op)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in _METHODS
    ]


def _assert_exact(
    found: set[tuple[str, str]], allowed: frozenset[tuple[str, str]], what: str
) -> None:
    new = sorted(found - allowed)
    stale = sorted(allowed - found)
    assert not new, f"{what}: param name collides with a body field (rename the param): {new}"
    assert not stale, f"{what}: allowlist entry no longer collides, remove it: {stale}"


def test_no_path_param_merges_with_a_body_field_in_the_same_n8n_resource(spec):
    ops = _ops(spec)
    body_by_tag: dict[str, set[str]] = defaultdict(set)
    for _, op in ops:
        body_by_tag[(op.get("tags") or ["?"])[0]] |= _body_props(spec, op)
    found = {
        (tag, name)
        for _, op in ops
        for tag in [(op.get("tags") or ["?"])[0]]
        for name in _params(spec, op, ("path",)) & body_by_tag[tag]
    }
    _assert_exact(found, SAME_MEANING_TAG_PATH_PARAMS, "path param vs body field in same tag")


def test_no_operation_param_shares_a_name_with_its_own_body_field(spec):
    found = {
        (op["operationId"], name)
        for _, op in _ops(spec)
        for name in _params(spec, op, ("path", "query")) & _body_props(spec, op)
    }
    _assert_exact(found, SAME_MEANING_SAME_OP_PARAMS, "param vs own request body")


def test_no_pm_projects_param_is_named_like_a_project_field(spec):
    project_fields = _props(spec, {"$ref": "#/components/schemas/ProjectIn"})
    assert "version" in project_fields  # the field the original collision clobbered
    found = {
        (op["operationId"], name)
        for path, op in _ops(spec)
        if path.startswith("/pm/projects")
        for name in _params(spec, op, ("path", "query")) & project_fields
    }
    _assert_exact(found, SAME_MEANING_PROJECT_PARAMS, "/pm/projects param vs ProjectIn")

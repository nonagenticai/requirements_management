"""OpenAPI of the modeling families is usable for client generation (final review, 8).

Every documented error response of ``/modeling``, ``/uml`` and ``/architecture`` routes
has the wire envelope (``ErrorResponse``) as its JSON schema — so a generated client gets
``ErrorDetail.head`` on a 412 and ``findings`` on a ``model_integrity`` 422 — every route
that accepts ``If-Match`` documents its ``invalid_if_match`` 422, and the model list's
filters are typed.
"""

from __future__ import annotations

from typing import Any

import pytest

from main import app

FAMILIES = ("/modeling", "/uml", "/architecture")


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    app.openapi_schema = None
    return app.openapi()


def _ops(spec: dict[str, Any]):
    for path, ops in spec["paths"].items():
        if path.startswith(FAMILIES):
            for method, op in ops.items():
                yield path, method, op


def _ref(response: dict[str, Any]) -> str | None:
    return (
        ((response.get("content") or {}).get("application/json") or {})
        .get("schema", {})
        .get("$ref")
    )


def test_every_error_response_is_the_error_envelope(spec: dict[str, Any]) -> None:
    examined, bad = 0, []
    for path, method, op in _ops(spec):
        for code, response in op.get("responses", {}).items():
            if code[0] in "45":
                examined += 1
                if _ref(response) != "#/components/schemas/ErrorResponse":
                    bad.append((method.upper(), path, code))
    assert examined > 300  # vacuity floor: the families are really examined
    assert bad == []


def test_a_412_carries_the_current_head(spec: dict[str, Any]) -> None:
    detail = spec["components"]["schemas"]["ErrorDetail"]["properties"]
    assert "head" in detail
    stale = [
        (m, p)
        for p, m, op in _ops(spec)
        if "412" in op.get("responses", {})
        and _ref(op["responses"]["412"]) == "#/components/schemas/ErrorResponse"
    ]
    assert len(stale) > 30


def test_if_match_routes_document_invalid_if_match(spec: dict[str, Any]) -> None:
    with_if_match, bad = 0, []
    for path, method, op in _ops(spec):
        if any(p.get("name") == "If-Match" for p in op.get("parameters", [])):
            with_if_match += 1
            r422 = op.get("responses", {}).get("422", {})
            if "invalid_if_match" not in r422.get("description", ""):
                bad.append((method.upper(), path))
    assert with_if_match > 30
    assert bad == []


def test_change_set_create_and_model_list_are_typed(spec: dict[str, Any]) -> None:
    create = spec["paths"]["/modeling/models/{model_id}/change-sets"]["post"]
    assert "422" in create["responses"]
    params = {
        p["name"]: p["schema"]
        for p in spec["paths"]["/modeling/models"]["get"]["parameters"]
        if "name" in p
    }
    language = params["language"]
    options = language.get("anyOf", [language])
    enums = [o.get("enum") for o in options if "enum" in o]
    assert enums == [["archimate", "uml"]], language
    q = params["q"]
    lengths = [o.get("maxLength") for o in q.get("anyOf", [q]) if "maxLength" in o]
    assert lengths == [200], q

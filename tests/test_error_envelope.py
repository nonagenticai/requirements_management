"""Error-envelope shape regression tests.

Every error response — 404 for an unknown path, 422 from request validation,
500 from an unhandled exception — must use the shared shape:

    {
        "error": {
            "code": "...",
            "message": "...",
            "status": <int>,
            "request_id": "<uuid7>",
            "timestamp": "YYYY-MM-DDTHH:MM:SSZ"
        }
    }

``details`` is gated behind ``LEAK_EXCEPTION_DETAILS`` (i.e. dev) on every path
that renders an EXCEPTION's own text — the 500 catch-all and the 504
statement-timeout path. The 422 request-validation path is the ONE exception and
is ungated: it emits only a redacted pydantic ``loc`` and the pydantic error
``type``, never ``input``, ``ctx`` or ``msg``. See ``main._validation_loc_types``.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

UUID7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# A licence for this tenant is minted by the conftest shim (no tokenless callers).
TENANT = {"X-Tenant-Id": "0195f48b-d000-7000-8000-000000000001"}
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _assert_envelope(body: dict, *, expected_status: int) -> None:
    assert set(body.keys()) == {"error"}, body
    err = body["error"]
    assert isinstance(err.get("code"), str) and err["code"], err
    assert isinstance(err.get("message"), str) and err["message"], err
    assert err.get("status") == expected_status, err
    assert UUID7_RE.match(err.get("request_id", "")), err
    assert TIMESTAMP_RE.match(err.get("timestamp", "")), err


async def test_handled_http_exception_uses_envelope() -> None:
    """A raised HTTPException from a route flows through the http handler.

    Mount a tiny app that registers the same exception handlers as main.py
    so the assertion isn't tied to the full requirements_management app
    boot (which requires postgres + license env vars).
    """
    from fastapi import FastAPI, HTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from main import http_exception_handler

    app = FastAPI()
    app.exception_handler(StarletteHTTPException)(http_exception_handler)

    @app.get("/boom")
    async def _boom() -> None:
        raise HTTPException(status_code=418, detail="i am a teapot")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/boom")

    assert resp.status_code == 418
    body = resp.json()
    _assert_envelope(body, expected_status=418)
    assert body["error"]["message"] == "i am a teapot"
    assert body["error"]["code"] == "HTTP_418"


async def test_404_envelope_via_handler() -> None:
    """An unknown path on a minimal app with the handler attached returns 404 envelope."""
    from fastapi import FastAPI
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from main import http_exception_handler

    app = FastAPI()
    app.exception_handler(StarletteHTTPException)(http_exception_handler)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/this-path-does-not-exist")

    assert resp.status_code == 404
    _assert_envelope(resp.json(), expected_status=404)


async def test_request_id_middleware_sets_header() -> None:
    """RequestIdMiddleware echoes a fresh uuid7 in X-Request-ID on every response."""
    from fastapi import FastAPI

    from middleware.request_id import RequestIdMiddleware

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ping")
    async def _ping() -> dict[str, str]:
        return {"ok": "yes"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping")

    assert resp.headers.get("X-Request-ID"), "X-Request-ID missing on response"
    assert UUID7_RE.match(resp.headers["X-Request-ID"])


# ⛔ These two used to be ONE test: a `@pytest.mark.skip` placeholder whose body was
# nothing but a docstring. It asserted nothing, had never run, and documented a
# SECURITY-relevant default — whether internal exception text reaches the client —
# entirely on trust. `LEAK_EXCEPTION_DETAILS` is read once at import
# (src/main.py), so the flag is patched on the module, not via the environment.
#
# ⚠️ MOVED OFF THE 422 PATH, deliberately, and the reason must survive:
# the 422 request-validation handler is now UNGATED (it emits a redacted `loc` +
# pydantic `type`, never a value), so asserting "no details with the flag off"
# there is no longer satisfiable by ANY correct implementation, and its ON arm
# would have become unable to fail. The gate itself is NOT being retired — these
# two arms now pin it on `_statement_timeout_to_response`, which renders an
# EXCEPTION'S OWN TEXT (`str(exc)`, a DB error carrying SQL and DSN fragments) and
# is therefore exactly the payload the gate exists for. It is unit-invocable, so
# neither arm needs the app, postgres or a licence.
#
# The ungated 422 path keeps its own pair below — one arm that the field name IS
# named, one that the offending VALUE is NOT.
def _timeout_details(monkeypatch: pytest.MonkeyPatch, *, leaking: bool) -> dict:
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", leaking)
    resp = main._statement_timeout_to_response(
        RuntimeError("canceling statement due to statement timeout\nDSN=postgresql://u:p@h/db"),
        request_id="0195f48b-d000-7000-8000-000000000001",
    )
    assert resp.status_code == 504
    body = json.loads(bytes(resp.body))
    _assert_envelope(body, expected_status=504)
    return body["error"]


async def test_details_is_absent_when_leaking_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production posture must not put internal exception text in the body.

    The flag is set EXPLICITLY rather than relying on the ambient value: under the
    test environment it is actually ON (tests/conftest.py sets it suite-wide), so a
    test that assumed the default would have been testing the opposite of
    production. Found by writing this arm and watching it fail.
    """
    error = _timeout_details(monkeypatch, leaking=False)
    assert "details" not in error, f"internal detail leaked with the flag off: {error!r}"


async def test_details_appears_when_leaking_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the dev flag on, the same abort DOES carry ``details``.

    This is the arm that makes the test above mean something: without it, a handler
    that never populates ``details`` under any circumstances would also pass.
    """
    error = _timeout_details(monkeypatch, leaking=True)
    assert "details" in error, f"flag on but no details: {error!r}"
    assert "statement timeout" in error["details"], (
        f"details present but not the exception text: {error['details']!r}"
    )


# ---------------------------------------------------------------------------
# The UNGATED 422 path: it must name the FIELD and must never echo the VALUE.
# ---------------------------------------------------------------------------
#
# ⛔ Every test below patches `LEAK_EXCEPTION_DETAILS` **OFF**. That is not
# decoration. tests/conftest.py sets the gate true suite-wide, so a *gated*
# implementation of this handler would make these tests green for the wrong
# reason — green would prove nothing about the production posture, which is the
# posture that left 15 AA cards blocked on a 422 that named no field.


async def test_the_422_names_the_offending_field_with_the_gate_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 422 must name WHICH field failed and HOW, in production posture."""
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=TENANT) as ac:
        # A body that cannot validate -> RequestValidationError -> 422 envelope.
        resp = await ac.post("/requirements", json={"nope": True})

    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR", error
    assert error["message"] == "Request validation failed", error
    details = error.get("details")
    assert details, f"a 422 that names no field is the whole defect: {error!r}"
    # `title`, `description` and `effort` are all required on RequirementIn.
    for field in ("body.title", "body.description", "body.effort"):
        assert field in details, f"{field} not named in {details!r}"
    assert "missing" in details, f"the pydantic error type is not named in {details!r}"


async def test_the_422_never_echoes_the_offending_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ THE LOAD-BEARING ARM. A leak here is a security regression.

    `title` is capped at 300 chars, so an over-long title produces a
    `string_too_long` whose pydantic `input` is the ENTIRE string the caller sent
    and whose `ctx` carries the limit. Neither may reach the response body.
    """
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)

    canary = "leakcanary9f3a"
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=TENANT) as ac:
        resp = await ac.post(
            "/requirements",
            json={
                "title": canary + ("x" * 400),
                "description": "d",
                "effort": 1.0,
            },
        )

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"].get("details")
    # Precondition, asserted rather than assumed: without it this test would pass
    # just as happily against a handler that emits no details at all.
    assert details and "body.title" in details, f"the failing field is not named: {details!r}"
    assert "string_too_long" in details, f"the error type is not named: {details!r}"
    # The value, in every form it could arrive in.
    assert canary not in resp.text, f"the caller's value leaked: {resp.text!r}"
    assert "xxxx" not in resp.text, f"the caller's value leaked: {resp.text!r}"
    # `ctx` for string_too_long is {"max_length": 300} — server config, also out.
    assert "300" not in details, f"pydantic ctx leaked: {details!r}"
    assert "max_length" not in details, f"pydantic ctx leaked: {details!r}"


async def test_a_caller_supplied_mapping_key_is_redacted_from_the_loc_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ The one place a `loc` is NOT value-free, exercised end to end.

    `RequirementIn.links` is `dict[str, list[str]]`, so a bad value under a
    caller's own key produces `loc == ("body", "links", "<their key>")`. A bare
    `".".join(loc)` would put that key in the response. Driven through the real
    app rather than a hand-built `loc` so the "body" prefix is FastAPI's, not the
    test's — the redaction depth depends on that prefix being there.
    """
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)

    canary_key = "leakcanarykey9f3a"
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=TENANT) as ac:
        resp = await ac.post(
            "/requirements",
            json={
                "title": "t",
                "description": "d",
                "effort": 1.0,
                "links": {canary_key: "not-a-list"},
            },
        )

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"].get("details")
    assert details and "body.links" in details, f"the failing field is not named: {details!r}"
    assert canary_key not in resp.text, f"a caller-supplied mapping key leaked: {resp.text!r}"
    assert "body.links.*" in details, f"the key segment was not redacted: {details!r}"


def test_the_literal_error_vocabulary_never_reaches_the_client() -> None:
    """A `literal_error`'s `ctx` enumerates every permitted value. It stays out.

    Unit-level on purpose: `POST /architecture/elements` needs a licence, and the
    property under test is a property of the RENDERER, not of the route.
    """
    from fastapi.exceptions import RequestValidationError
    from pydantic import ValidationError

    import main
    from schemas.architecture import ArchitectureElementIn

    with pytest.raises(ValidationError) as exc_info:
        # `model_validate` on a plain dict rather than keyword arguments: the
        # values are deliberately outside the `Literal`, and `ty` flags that
        # statically at a keyword call site (see 091f684:tests/test_coverage_boost.py:1207
        # for the same collision) even though triggering it is the whole point.
        ArchitectureElementIn.model_validate(
            {
                "title": "t",
                "layer": "canarylayer9f3a",
                "element_type": "canarytype9f3a",
            }
        )

    raw = exc_info.value.errors()
    # Precondition: pydantic really does hand us the vocabulary. If a future
    # pydantic stops doing so, this test must be re-established, not deleted.
    expected = [str(err.get("ctx", {}).get("expected", "")) for err in raw]
    assert any("'" in e for e in expected), (
        f"precondition failed: no literal vocabulary in ctx, got {expected!r}"
    )

    rendered = main._validation_loc_types(
        RequestValidationError([{**err, "loc": ("body", *err["loc"])} for err in raw])
    )

    assert "body.layer:literal_error" in rendered, rendered
    assert "canarylayer9f3a" not in rendered, f"the caller's value leaked: {rendered!r}"
    for vocabulary in expected:
        for token in vocabulary.replace("'", "").split(", "):
            assert token and token not in rendered, (
                f"the permitted vocabulary leaked via ctx: {token!r} in {rendered!r}"
            )


def test_safe_loc_keeps_indices_and_redacts_only_deep_string_segments() -> None:
    """The FALLBACK rule (no body type to walk), pinned shape by shape.

    Without this, a change that redacted EVERYTHING (or nothing past depth 1)
    would still satisfy the end-to-end tests above.
    """
    import main

    assert main._safe_loc(("body", "title")) == "body.title"
    assert main._safe_loc(("query", "limit")) == "query.limit"
    # Bulk endpoints post a list: the index is positional, and the field name
    # after it is DECLARED, so both survive.
    assert main._safe_loc(("body", 0, "title")) == "body.0.title"
    # A caller-supplied mapping key, with and without a list index under it.
    assert main._safe_loc(("body", "links", "secret")) == "body.links.*"
    assert main._safe_loc(("body", "links", "secret", 0)) == "body.links.*.0"
    assert main._safe_loc(("body", 0, "links", "secret")) == "body.0.links.*"
    assert main._safe_loc(()) == "unknown"


# ---------------------------------------------------------------------------
# Schema-walked paths: a 422 names the REAL nested field, never a value.
# ---------------------------------------------------------------------------
#
# `POST /uml/tools/validate` takes `ValidateUmlRequest` — `elements:
# list[UmlElementIn]`, each with a strict (`extra="forbid"`) nested
# `attributes: UmlElementAttributes`. Before the walk, both errors below rendered
# as `body.elements.0.*.*`, which named nothing an agent could act on.


async def _post_uml(body: dict) -> httpx.Response:
    import main

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=TENANT) as ac:
        return await ac.post("/uml/tools/validate", json=body)


async def test_a_nested_extra_forbidden_key_names_its_full_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)
    canary = "leakcanaryvalue9f3a"
    resp = await _post_uml(
        {"elements": [{"id": "e1", "kind": "class", "attributes": {"note": canary}}]}
    )

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"].get("details")
    assert details == "body.elements.0.attributes.note:extra_forbidden", details
    assert canary not in resp.text, f"the caller's value leaked: {resp.text!r}"


async def test_a_nested_type_error_names_its_full_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)
    canary = "leakcanaryvalue9f3a"
    resp = await _post_uml(
        {
            "elements": [
                {
                    "id": "e1",
                    "kind": "class",
                    "attributes": {"properties": [{"id": "p1", "is_static": canary}]},
                }
            ]
        }
    )

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"].get("details")
    assert details == "body.elements.0.attributes.properties.0.is_static:bool_parsing", details
    assert canary not in resp.text, f"the caller's value leaked: {resp.text!r}"


async def test_an_extra_key_that_is_not_name_shaped_is_still_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undeclared key is shown only if it looks like a field name, not content."""
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)
    canary_key = "leak canary key: 9f3a"
    resp = await _post_uml({"elements": [{"id": "e1", "kind": "class", canary_key: 1}]})

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"].get("details")
    assert details == "body.elements.0.*:extra_forbidden", details
    assert "canary" not in resp.text, f"a caller-supplied key leaked: {resp.text!r}"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("notes", "body.notes:extra_forbidden"),
        ("leak canary: secret value 123", "body.*:extra_forbidden"),
        ("k" * 65, "body.*:extra_forbidden"),
    ],
)
async def test_a_top_level_extra_key_is_shown_only_when_name_shaped(
    monkeypatch: pytest.MonkeyPatch, key: str, expected: str
) -> None:
    """The first field after `body` is kept unconditionally EXCEPT an extra key.

    On every `extra="forbid"` body model a top-level unknown key is caller text;
    without the name-shape check it was echoed verbatim at any length.
    """
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)
    resp = await _post_uml({"elements": [], key: 1})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"].get("details") == expected, resp.text
    if expected.startswith("body.*"):
        assert key not in resp.text, f"a caller-supplied key leaked: {resp.text!r}"


def test_the_depth_fallback_also_hides_a_non_name_shaped_extra_key() -> None:
    import main

    assert main._safe_loc(("body", "a b: c"), None, "extra_forbidden") == "body.*"
    assert main._safe_loc(("body", "notes"), None, "extra_forbidden") == "body.notes"


async def test_a_walk_that_raises_falls_back_to_the_depth_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 422 must never become a 500 because the schema walk hit something odd."""
    import main

    def _boom(tp: object, segment: object) -> tuple[bool, object]:
        raise RuntimeError("walk exploded")

    monkeypatch.setattr(main, "_step", _boom)
    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)
    canary = "leakcanaryvalue9f3a"
    resp = await _post_uml(
        {"elements": [{"id": "e1", "kind": "class", "attributes": {"note": canary}}]}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"].get("details") == "body.elements.0.*.*:extra_forbidden", resp.text
    assert canary not in resp.text


def test_safe_loc_walks_the_schema_and_still_redacts_mapping_keys() -> None:
    """With a body type, declared names survive at any depth; mapping keys never do."""
    import main
    from routers.uml_tools import ValidateUmlRequest
    from schemas.requirements import RequirementIn

    loc = ("body", "elements", 0, "attributes", "operations", 1, "parameters", 0, "name")
    assert main._safe_loc(loc, ValidateUmlRequest, "string_type") == ".".join(map(str, loc))
    # `links` is dict[str, list[str]]: the key is caller data even when the walk
    # knows the type, and the index under it is still named.
    assert main._safe_loc(("body", "links", "secret", 0), RequirementIn, "string_type") == (
        "body.links.*.0"
    )
    # A mapping key is redacted even on an extra_forbidden error.
    assert main._safe_loc(("body", "links", "secret"), RequirementIn, "extra_forbidden") == (
        "body.links.*"
    )
    # An undeclared segment on a non-extra error is not guessed at.
    assert main._safe_loc(("body", "elements", 0, "bogus"), ValidateUmlRequest, "missing") == (
        "body.elements.0.*"
    )


# ---------------------------------------------------------------------------
# The BOUND on the ungated 422 path: `details` must not be caller-proportional.
# ---------------------------------------------------------------------------
#
# One `loc:type` pair is rendered per pydantic error and a CALLER decides how
# many errors there are, so before `main._MAX_VALIDATION_LOC_TYPES` this response
# grew with the request: an audit measured a 5,000-key `links` body turning a
# 53,950 B request into a 120,186 B response. These three pin the bound, the
# honesty of the truncation marker, and — the load-bearing one — that truncating
# did not open a leak the un-truncated path did not have.
#
# The cap VALUE is read from the module rather than hardcoded here on purpose:
# the contract is "bounded, first-errors-first, and it says what it dropped".
# The particular number is a policy choice that must be free to move without
# these tests going red for the wrong reason.


def _missing_errors(count: int) -> list[dict]:
    """`count` distinguishable pydantic-shaped errors, in a known order.

    `loc == ("body", i, "title")` renders as `body.<i>.title` — an int segment
    and a DECLARED field name, so `_safe_loc` redacts nothing and each pair is
    distinct. That distinctness is what makes "the pairs kept are the FIRST ones"
    an assertable property; a 40-key `links` body (the real amplification shape)
    yields 40 errors that all render `body.links.*` and are therefore
    indistinguishable from each other. The real shape is exercised end to end by
    `test_a_truncated_422_still_carries_no_caller_value` below.
    """
    return [
        {"type": "missing", "loc": ("body", i, "title"), "msg": "Field required"}
        for i in range(count)
    ]


def test_the_422_details_truncates_past_the_cap_and_says_how_many_it_dropped() -> None:
    """Past the cap: exactly `cap` pairs, the FIRST ones, plus an explicit count."""
    from fastapi.exceptions import RequestValidationError

    import main

    cap = main._MAX_VALIDATION_LOC_TYPES
    dropped = 15
    raw = _missing_errors(cap + dropped)
    # Precondition, asserted rather than assumed: the fixture really does hand the
    # renderer more errors than the cap. Without this the test would pass against
    # any cap larger than the fixture.
    assert len(raw) > cap, f"fixture does not exceed the cap: {len(raw)} <= {cap}"

    rendered = main._validation_loc_types(RequestValidationError(raw))

    assert rendered.endswith(f", ... (+{dropped} more)"), (
        f"no truncation marker, or the wrong omitted count: {rendered!r}"
    )
    head = rendered.split(", ... (+")[0]
    pairs = head.split(", ")
    assert len(pairs) == cap, f"expected {cap} pairs, got {len(pairs)}: {rendered!r}"
    # Ordering AND retention in one assertion: the pairs kept are pydantic's first
    # `cap` errors, in pydantic's own order. A non-deterministic subset would make
    # the field useless for diagnosis and this assertion unwritable.
    assert pairs == [f"body.{i}.title:missing" for i in range(cap)], rendered
    # The first error PAST the cap is gone, not merely reordered.
    assert f"body.{cap}.title" not in rendered, f"a past-cap pair survived: {rendered!r}"


async def test_a_sub_cap_422_is_not_truncated_and_carries_no_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At or under the cap, nothing is dropped and no marker is invented.

    Two arms, because they fail to different mutations: the app arm is the
    ordinary three-missing-fields 422 a caller actually sees, and the unit arm
    sits EXACTLY on the cap, which is where an off-by-one in the slice lives.
    """
    from fastapi.exceptions import RequestValidationError

    import main

    # ARM 1 — end to end, in production posture. `LEAK_EXCEPTION_DETAILS` is
    # patched OFF for the reason the block above this one gives: conftest pins the
    # gate true suite-wide, so a gated implementation would pass for the wrong
    # reason.
    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=TENANT) as ac:
        resp = await ac.post("/requirements", json={"nope": True})

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"].get("details")
    assert details, f"a 422 that names no field is the whole defect: {resp.text!r}"
    assert "more)" not in details, f"a sub-cap 422 was marked truncated: {details!r}"
    assert "..." not in details, f"a sub-cap 422 was marked truncated: {details!r}"
    # Still the complete list, so the bound did not cost the diagnosability.
    for field in ("body.title", "body.description", "body.effort"):
        assert field in details, f"{field} not named in {details!r}"

    # ARM 2 — exactly ON the cap. Nothing is omitted, so nothing is marked.
    cap = main._MAX_VALIDATION_LOC_TYPES
    at_cap = main._validation_loc_types(RequestValidationError(_missing_errors(cap)))
    assert "more)" not in at_cap, f"a list exactly at the cap was truncated: {at_cap!r}"
    assert len(at_cap.split(", ")) == cap, f"expected {cap} pairs: {at_cap!r}"
    assert at_cap.endswith(f"body.{cap - 1}.title:missing"), (
        f"the last in-cap pair was dropped: {at_cap!r}"
    )


async def test_a_truncated_422_still_carries_no_caller_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⭐ THE LOAD-BEARING ARM. Truncation must not open a leak.

    Driven through the real app in the exact shape the audit measured — a `links`
    mapping with many caller-chosen keys, plus an over-long `title` — so the
    errors are FastAPI's and the `loc` prefixes are real. Every caller-controlled
    string in the request is a canary, and the truncation marker is matched by a
    STRICT regex: the only caller-derived thing it may carry is a count.
    """
    import main

    monkeypatch.setattr(main, "LEAK_EXCEPTION_DETAILS", False)

    key_canary = "leakcanarykey7b2e"
    value_canary = "leakcanaryval7b2e"
    keys = {f"{key_canary}{i}": "not-a-list" for i in range(40)}

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=TENANT) as ac:
        resp = await ac.post(
            "/requirements",
            json={
                "title": value_canary + ("x" * 400),
                "description": "d",
                "effort": 1.0,
                "links": keys,
            },
        )

    assert resp.status_code == 422, resp.text
    details = resp.json()["error"].get("details")
    # Preconditions: this really is a TRUNCATED response from the real path.
    # Without them a handler that emitted no details at all would pass every
    # leak assertion below, vacuously.
    assert details, f"a 422 that names no field is the whole defect: {resp.text!r}"
    assert "body.links.*" in details, f"the failing field is not named: {details!r}"
    assert re.search(r", \.\.\. \(\+\d+ more\)$", details), (
        f"not truncated, so this test proves nothing about truncation: {details!r}"
    )

    # ⛔ The marker's only variable part is a decimal count — no caller data can
    # ride along in it.
    marker = re.search(r", \.\.\. \(\+\d+ more\)$", details)
    assert marker is not None
    assert re.fullmatch(r", \.\.\. \(\+\d+ more\)", marker.group(0)), marker.group(0)

    # The leak assertions, against the WHOLE response, not just `details`.
    assert key_canary not in resp.text, f"a caller-supplied mapping key leaked: {resp.text!r}"
    assert value_canary not in resp.text, f"the caller's value leaked: {resp.text!r}"
    assert "xxxx" not in resp.text, f"the caller's value leaked: {resp.text!r}"
    assert "not-a-list" not in resp.text, f"the caller's value leaked: {resp.text!r}"
    # Truncation changed nothing else about the envelope.
    _assert_envelope(resp.json(), expected_status=422)
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR", resp.text
    assert resp.json()["error"]["message"] == "Request validation failed", resp.text

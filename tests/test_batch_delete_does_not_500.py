"""A well-formed batch-delete request answered HTTP 500. It is not a fault.

`POST /architecture/elements/batch-delete` takes exactly one of `ids` or
`filter`. ``{"ids": []}`` passes the mutual-exclusion guard (``[]`` is not None,
and ``[] and None`` is falsy), then a bare ``if req.ids:`` skipped the ids branch
on an empty list and fell through to ``assert req.filter is not None`` on the
filter path. An ``AssertionError`` is a 500 — "the server is broken" for a
request the server understood perfectly. Deleting an empty set is a no-op.

⚠️ A SECOND defect I filed here was REFUTED before it was fixed. I believed an
unknown `filter` key reached `getattr(ArchitectureElement, key)` unchecked and
raised `AttributeError` -> 500. It does not: the request model already rejects
unknown keys with a 422 naming the allowed set. The router-side allowlist I had
written was dead code AND wider than the real one (34 mapped columns against the
schema's 4), so it would have read as protection while protecting nothing. It is
reverted; these tests keep the 422 pinned so the schema-level guard cannot be
removed silently.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

_URL = "/architecture/elements/batch-delete"


async def test_an_empty_ids_list_is_a_no_op_not_a_500(
    authenticated_client: AsyncClient, test_session
) -> None:
    """It deletes nothing and says so, rather than raising through the router."""
    resp = await authenticated_client.post(_URL, json={"ids": []})

    assert resp.status_code != 500, resp.text
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 0, body
    assert body["ids"] == [], body


async def test_an_unknown_filter_field_is_422_at_the_schema_not_500(
    authenticated_client: AsyncClient, test_session
) -> None:
    """The SCHEMA rejects it, before the router ever calls getattr.

    Pins the guard I would otherwise have duplicated in the router.

    ⚠️ THIS TEST USED TO ASSERT ON THE RESPONSE BODY and the two things it looked
    for were `"definitely_not_a_column"` (the CALLER's own filter key) and
    `"layer"` (the whole allowed-key VOCABULARY, from the ValueError message).
    Both only appeared because tests/conftest.py sets `LEAK_EXCEPTION_DETAILS`
    true suite-wide — in production, with the gate off, the 422 carried no
    `details` at all, so what this asserted was never the production behaviour.
    The 422 path is now ungated and emits a redacted `loc` + pydantic error
    `type` ONLY, deliberately dropping `msg`/`ctx`/`input`
    (`main._validation_loc_types`), so neither string can be on the wire again.

    The assertion was NOT weakened, it was MOVED: the full-strength
    "the schema names the unknown key and lists what is filterable" check now
    runs against the schema directly, below, where the text is a SERVER-SIDE
    error that never reaches a client. What stays here is the part that is
    genuinely about HTTP — 422 rather than 500, refused at the schema (a pydantic
    `value_error` on `filter`) rather than by the router.
    """
    resp = await authenticated_client.post(_URL, json={"filter": {"definitely_not_a_column": "x"}})

    assert resp.status_code != 500, resp.text
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR", error
    details = error.get("details")
    # `body.filter` and not, say, `body.ids`: the refusal is about the field the
    # caller actually got wrong, and `value_error` says a field VALIDATOR refused
    # it — which is the schema-level guard this test exists to pin.
    assert details == "body.filter:value_error", details
    # ⛔ And the response must not carry either string, whatever the gate says.
    assert "definitely_not_a_column" not in resp.text, resp.text
    assert "Allowed:" not in resp.text, resp.text


async def test_the_schema_error_names_the_unknown_key_and_the_allowed_set() -> None:
    """Server-side, the refusal is fully actionable — it just is not shipped.

    This is the arm that carries what the HTTP test above no longer can. Without
    it, a validator that refused every filter with a bare "invalid" would still
    satisfy every other test in this file.
    """
    from pydantic import ValidationError

    from routers.architecture_elements import BatchDeleteRequest

    with pytest.raises(ValidationError) as exc_info:
        BatchDeleteRequest(filter={"definitely_not_a_column": "x"})

    message = str(exc_info.value)
    assert "definitely_not_a_column" in message, message
    # The message is actionable: it lists what IS filterable.
    for allowed in ("layer", "status", "project_id", "is_synthetic"):
        assert allowed in message, message


async def test_a_dunder_filter_field_is_refused(
    authenticated_client: AsyncClient, test_session
) -> None:
    """`__class__` resolves via getattr but is not a filterable key."""
    resp = await authenticated_client.post(_URL, json={"filter": {"__class__": "x"}})

    assert resp.status_code != 500, resp.text
    assert resp.status_code == 422, resp.text


async def test_a_real_filter_field_still_works(
    authenticated_client: AsyncClient, test_session
) -> None:
    """⛔ The control. Without it, "400 on every filter" would pass the three above.

    A filter naming a genuine column must still be accepted and execute — this is
    what separates "the allowlist is correct" from "the allowlist rejects
    everything".
    """
    resp = await authenticated_client.post(_URL, json={"filter": {"layer": "no-such-layer-value"}})

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 0, resp.text


async def test_neither_ids_nor_filter_is_still_400(
    authenticated_client: AsyncClient, test_session
) -> None:
    """The pre-existing guard must survive the `is not None` change."""
    resp = await authenticated_client.post(_URL, json={})
    assert resp.status_code == 400, resp.text

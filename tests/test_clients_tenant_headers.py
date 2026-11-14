"""The outbound clients in ``src/clients/`` must send X-Tenant-Id unconditionally.

``coding_analysis.fetch_code_units`` (and the since-removed
``coding_ai.fetch_proposals``) used to attach the tenant header only ``if tenant_id:``::

    headers: dict[str, str] = {}
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    resp = await client.get(url, headers=headers or None)

With a falsy tenant the request went out with **no headers at all** and was
accepted only because coding-analysis / coding-ai fall back to trusting any
RFC1918 source IP — which covers the entire pod CIDR. That fallback is being
emptied service by service (coding-analysis and coding-security are already
closed), so a header-less call starts 401ing; and both call sites swallow or
degrade upstream failures, so the breakage would be near-invisible.

Neither gets a ``DEFAULT_TENANT_ID`` fallback, unlike the fire-and-forget scan
kickoffs in coding-github#61 / coding-realtime#35: these responses are
tenant-scoped data returned to the caller, so a guessed tenant would answer one
tenant's request out of another tenant's data. The honest answer to an unknown
tenant is to fail loudly at the call site — same reasoning as the snapshot
writer in coding-analysis#75.

Two things are asserted per client, and neither can pass vacuously:
  * the header is on a REAL httpx request built by the REAL shared client
    (a dropped header makes the lookup raise KeyError, not silently pass);
  * a falsy tenant raises ValueError and performs NO HTTP call at all.

2026-08-15 — the tenant header alone stopped being enough
---------------------------------------------------------
coding now runs ``TRUST_TENANT_HEADER=false`` on all nine services, so a bare
``X-Tenant-Id`` proves nothing and both of these calls 401'd in production
(driven from the requirements pod against the live services). The fix presents
this service's own licence in ``Authorization``; coding honours the asserted
tenant because that subject is on its ``CODING_ONBEHALF_SUBJECTS`` allowlist.

The tests below therefore assert the **two headers together, on one request**.
Separately they are both satisfiable by the defect that matters: putting the
service token into ``X-Tenant-Id`` (what ``outbound_headers(service_token=...)``
does) authenticates fine and returns 200 — for the *service's* tenant, silently
serving the wrong customer's data. Only asserting that the tenant header still
carries the CUSTOMER's tenant *while* Authorization carries the service token
can distinguish the fix from that mutant.
"""

from __future__ import annotations

import httpx
import pytest

from clients import coding_analysis
from tests.tenants import T

# Opaque, zero-dot — the real shape of a licence token, and the shape that makes
# the `>=2 dots ⇒ JWT` heuristic in outbound_headers' tenant ladder the wrong
# branch. Deliberately NOT JWT-shaped.
_SERVICE_TOKEN = "svc-requirements-test-token"

# Deliberately NOT the production default: this asserts the value is PIPED from
# settings onto the wire, not that the default happens to be hardcoded somewhere.
# `test_app_id_default_is_the_requirements_module` pins the default separately.
_APP_ID = "app-requirements-test"

_CLIENTS = (
    pytest.param(
        coding_analysis,
        "fetch_code_units",
        "coding_analysis_url",
        "get_coding_analysis_client",
        id="coding_analysis.fetch_code_units",
    ),
)


def _install_settings(monkeypatch, *, service_token: str, app_id: str = _APP_ID):
    """Point the client at a dummy base URL, with a chosen service token + app id."""
    import settings as settings_module

    real = settings_module.get_settings()

    class _Stub:
        coding_analysis_url = "http://coding-analysis.test"
        coding_service_token = service_token
        requirements_module_app_id = app_id

        def __getattr__(self, name):  # everything else defers to the real settings
            return getattr(real, name)

    stub = _Stub()
    monkeypatch.setattr(settings_module, "get_settings", lambda: stub)
    return stub


@pytest.fixture
def _configured(monkeypatch):
    """Configured WITH a service token — the production shape."""
    return _install_settings(monkeypatch, service_token=_SERVICE_TOKEN)


@pytest.fixture
def _configured_without_token(monkeypatch):
    """Configured with CODING_SERVICE_TOKEN unset."""
    return _install_settings(monkeypatch, service_token="")


def _capturing_client(sent: list[httpx.Request]) -> httpx.AsyncClient:
    """A real httpx.AsyncClient whose transport records the request it builds.

    The header assertion therefore runs against a genuine, fully-built
    ``httpx.Request`` — not against a dict the test constructed itself.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"container": "c", "proposals": [], "components": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@pytest.mark.tenant_isolation
@pytest.mark.parametrize(("module", "func_name", "_url_attr", "client_getter"), _CLIENTS)
async def test_sends_tenant_header(
    module, func_name, _url_attr, client_getter, monkeypatch, _configured
):
    """A present tenant lands on the wire as X-Tenant-Id."""
    sent: list[httpx.Request] = []
    monkeypatch.setattr(module, client_getter, lambda *a, **k: _capturing_client(sent))

    await getattr(module, func_name)("some-container", tenant_id=T("tenant-a"))

    assert len(sent) == 1, "expected exactly one upstream request"
    # Indexing (not .get) — a dropped header must raise, never read as None.
    assert sent[0].headers["x-tenant-id"] == T("tenant-a")


@pytest.mark.tenant_isolation
@pytest.mark.security
@pytest.mark.parametrize(("module", "func_name", "_url_attr", "client_getter"), _CLIENTS)
async def test_sends_service_credential_without_clobbering_the_tenant(
    module, func_name, _url_attr, client_getter, monkeypatch, _configured
):
    """The service identity rides Authorization; the CUSTOMER tenant stays bare.

    Both headers, on one request, is the whole contract — because the dangerous
    mutant satisfies each of them separately.

    That mutant is ``outbound_headers(service_token=…)`` in place of
    ``authorization_service_token=…``: one keyword apart, and it routes the
    credential through the ``X-Tenant-Id`` ladder instead. It authenticates, it
    returns 200, and it answers out of the SERVICE's tenant — a data-isolation
    bug wearing a success. Against a version of it that ALSO sets Authorization,
    an Authorization-only assertion passes; an ``x-tenant-id``-only assertion
    passed even before this fix existed. Pinning the tenant header to the
    customer value *while* Authorization carries the token is what discriminates.
    """
    sent: list[httpx.Request] = []
    monkeypatch.setattr(module, client_getter, lambda *a, **k: _capturing_client(sent))

    await getattr(module, func_name)("some-container", tenant_id=T("tenant-a"))

    assert len(sent) == 1, "expected exactly one upstream request"
    req = sent[0]
    # Indexing, not .get — a dropped header must raise, never read as None.
    assert req.headers["authorization"] == f"Bearer {_SERVICE_TOKEN}"
    assert req.headers["x-tenant-id"] == T("tenant-a"), (
        "the customer tenant must survive verbatim; finding the service token "
        "here means the credential was routed through the tenant ladder and "
        "every request would resolve to the service's own tenant"
    )
    assert _SERVICE_TOKEN not in req.headers["x-tenant-id"]
    # Attribution, third axis: X-App-Id names WHICH application asked. It is not
    # a credential and must never be routed through Bearer or the tenant ladder.
    assert req.headers["x-app-id"] == _APP_ID


@pytest.mark.tenant_isolation
@pytest.mark.parametrize(("module", "func_name", "_url_attr", "client_getter"), _CLIENTS)
async def test_app_id_is_omitted_when_unset(
    module, func_name, _url_attr, client_getter, monkeypatch
):
    """NEGATIVE CONTROL: an empty app id sends NO header — it does not invent one.

    Billing distinguishes "not provided" from "provided as the empty string", and
    the only honest encoding of the former on the wire is the absence of the key.
    A service stamping its own id here would collapse every distinct upstream
    origin into one bucket while reading as 100% coverage.
    """
    _install_settings(monkeypatch, service_token=_SERVICE_TOKEN, app_id="")
    sent: list[httpx.Request] = []
    monkeypatch.setattr(module, client_getter, lambda *a, **k: _capturing_client(sent))

    await getattr(module, func_name)("some-container", tenant_id=T("tenant-a"))

    assert len(sent) == 1
    assert "x-app-id" not in sent[0].headers


def test_app_id_default_is_the_requirements_module() -> None:
    """The default is the registered Requirements Module catalogue app id.

    Pinned because it is real attribution data, not a placeholder: it is what
    worker_management calls sent before the LLM lane was removed (#238) and what
    gitops bills as the Requirements Module. A change here changes billing.
    """
    from settings import Settings

    assert Settings.model_fields["requirements_module_app_id"].default == (
        "06954722-ce8f-7ff3-80c2-23e49a36e26f"
    )


@pytest.mark.tenant_isolation
@pytest.mark.parametrize(("module", "func_name", "_url_attr", "client_getter"), _CLIENTS)
async def test_no_credential_configured_sends_no_authorization(
    module, func_name, _url_attr, client_getter, monkeypatch, _configured_without_token
):
    """NEGATIVE CONTROL: an unset token omits the header — it does not invent one.

    Without this, a helper that emitted ``Authorization: Bearer `` (empty) or
    fell back to some other value would pass the positive test above. The honest
    behaviour for a missing credential is the pre-fix shape, which 401s loudly
    and identically everywhere.
    """
    sent: list[httpx.Request] = []
    monkeypatch.setattr(module, client_getter, lambda *a, **k: _capturing_client(sent))

    await getattr(module, func_name)("some-container", tenant_id=T("tenant-a"))

    assert len(sent) == 1
    assert "authorization" not in sent[0].headers
    assert sent[0].headers["x-tenant-id"] == T("tenant-a")


@pytest.mark.tenant_isolation
@pytest.mark.parametrize("bad_tenant", [None, ""], ids=["none", "empty"])
@pytest.mark.parametrize(("module", "func_name", "_url_attr", "client_getter"), _CLIENTS)
async def test_missing_tenant_raises_and_never_calls_upstream(
    module, func_name, _url_attr, client_getter, bad_tenant, monkeypatch, _configured
):
    """NEGATIVE CONTROL: a falsy tenant is a loud error, not a header-less request.

    This is the assertion the pre-fix code fails: it happily issued the call with
    no headers and let the receiver's source-IP trust bypass wave it through.
    """
    sent: list[httpx.Request] = []
    monkeypatch.setattr(module, client_getter, lambda *a, **k: _capturing_client(sent))

    with pytest.raises(ValueError, match="requires a tenant_id"):
        await getattr(module, func_name)("some-container", tenant_id=bad_tenant)

    assert sent == [], "a tenant-less call must never reach the upstream service"


@pytest.mark.tenant_isolation
@pytest.mark.parametrize(("module", "func_name", "_url_attr", "client_getter"), _CLIENTS)
def test_tenant_id_is_a_required_keyword(module, func_name, _url_attr, client_getter):
    """tenant_id must have no default — omitting it is a TypeError, not a silent None.

    The runtime guard above only fires once someone passes a falsy value. Making
    the parameter required is what stops a *new* call site from omitting it
    entirely, and is what lets `ty` catch the omission before CI even runs.
    """
    import inspect

    param = inspect.signature(getattr(module, func_name)).parameters["tenant_id"]
    assert param.default is inspect.Parameter.empty, "tenant_id must not default to None"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.annotation == "str", "tenant_id must be str, not str | None"

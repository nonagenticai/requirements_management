"""FastAPI application factory."""

import logging
import os
import re
import time
import uuid
from collections.abc import Iterable, Mapping, MutableMapping, MutableSequence, Sequence
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from __init__ import __version__
from auth.middleware import AuthMiddleware, is_public_path
from lifespan import lifespan
from logging_config import configure_logging
from middleware import add_request_logging_middleware, add_security_headers_middleware
from middleware.request_id import RequestIdMiddleware
from routers import register_routers
from schemas.modeling import INTEGRITY_ERROR_TYPE

configure_logging()
logger = logging.getLogger(__name__)

# Licence token authentication is OPT-IN. Off by default so a fresh clone runs with no
# licences service and no configuration; a deployment that wants tenant isolation sets
# `ENABLE_AUTH=true` explicitly (the cluster manifest does). This matches every other
# repository in the estate: the gate is a switch, not a prerequisite.
ENABLE_AUTH = os.getenv("ENABLE_AUTH", "false").lower() == "true"
# ⛔ THE `""` DEFAULT HERE IS DELIBERATE AND MUST NOT BE UNIFIED WITH
# `settings.environment`, WHOSE DEFAULT IS `"development"`.
# This looks like an inconsistency and is a fail-safe. An ABSENT `ENVIRONMENT`
# must mean "do not leak", never "assume development". Sourcing this from
# `settings` would make a missing variable resolve to `"development"` and switch
# exception-detail leaking ON.
# `ENVIRONMENT` WAS absent from the production Deployment on 2026-09-04; measured
# 2026-09-05 it IS set there — `/health` reports `environment=production` and the
# production fail-fast guard in `settings.py` is enforced. That does not retire
# this default, it only means it is no longer the state we are in: it stays so
# that a variable later renamed, dropped, or absent in any other process that
# imports `main` fails CLOSED. The safe direction and the tidy direction point
# opposite ways here; safety wins.
# Pinned by tests/test_environment_is_named.py.
LEAK_EXCEPTION_DETAILS = os.getenv("LEAK_EXCEPTION_DETAILS", "").lower() == "true" or os.getenv(
    "ENVIRONMENT", ""
).lower() in {"development", "dev"}

app = FastAPI(
    title="Requirements Management API",
    description=(
        "Multi-tenant API for requirements engineering, enterprise architecture, and project "
        "management. Manage software requirements, architecture principles, and architecture "
        "decision records (ADRs); model enterprise architecture (elements, relationships, views, "
        "standards) with traceability and gap/impact analysis; and run project and portfolio "
        "management (programmes, projects, resources, risks and benefits). Supports full-text "
        "and semantic search, quality "
        "scoring, WSJF/RICE prioritization, version history with snapshot comparison, and bulk "
        "operations across all entity types."
    ),
    version=__version__,
    servers=[
        {
            "url": os.getenv("API_BASE_URL", "http://localhost:8921"),
            "description": "API Server",
        }
    ],
    lifespan=lifespan,
    redirect_slashes=False,
    swagger_ui_parameters={"persistAuthorization": True},
    # Fallback unique-ID generator. Every route still sets `operation_id=`
    # explicitly (pinned by tests/test_openapi.py); this exists so a forgotten ID
    # degrades to `<tag>_<func>` (readable) instead of FastAPI's default.
    generate_unique_id_function=lambda route: (
        f"{route.tags[0] if route.tags else 'default'}_{route.name}"
    ),
    # Declares every tag actually used on a route, in domain-grouped display
    # order. Names must match the route `tags=[...]` strings byte-for-byte or
    # the description will not bind. A drift guard
    # (tests/test_openapi.py::test_tag_groups_are_defined) fails CI if the
    # declared set ever diverges from the set used on operations.
    openapi_tags=[
        {"name": "Health", "description": "Liveness, readiness, dependency probes."},
        # Requirements / governance domain
        {"name": "Requirements", "description": "Software requirements CRUD."},
        {"name": "Principles", "description": "Architecture principles CRUD."},
        {
            "name": "Principle Conflicts",
            "description": (
                "Persisted governance verdicts: principles that conflict with each "
                "other or with agentic procedures (redundant / contradicts / violates)."
            ),
        },
        {"name": "Decisions", "description": "Architecture decision records (ADRs)."},
        {"name": "Search", "description": "Full-text and semantic search across entities."},
        {"name": "Quality", "description": "Quality scoring and quality-evaluation history."},
        {"name": "Priority", "description": "WSJF / RICE prioritization."},
        {"name": "Versions", "description": "Entity version history and snapshot comparison."},
        {"name": "Bulk Operations", "description": "Multi-entity batch operations."},
        # Logical Thinking Process (Theory of Constraints) domain
        {
            "name": "Logical Thinking Process",
            "description": (
                "Dettmer Logical-Thinking-Process structures (IO-Map, Evaporating "
                "Cloud, Current/Future Reality Trees, Prerequisite/Transition Trees): "
                "trees, nodes, necessity/sufficiency edges, and CLR scrutiny."
            ),
        },
        {
            "name": "TOC Config",
            "description": "Global → tenant → project config for Logical-Thinking-Process behaviour.",
        },
        # Enterprise-architecture domain
        {"name": "Architecture Elements", "description": "Enterprise-architecture model elements."},
        {
            "name": "Architecture Relationships",
            "description": "Relationships and traceability links between elements.",
        },
        {
            "name": "Architecture Views",
            "description": "Architecture viewpoints and generated views.",
        },
        {"name": "Architecture Standards", "description": "Standards catalog and compliance."},
        {
            "name": "Architecture Tools",
            "description": (
                "Traceability matrices, gap/impact analysis, ADM completeness, "
                "capability heatmaps, standards-compliance checks."
            ),
        },
        {
            "name": "Architecture Metamodel",
            "description": (
                "ArchiMate 3.2 reference data generated from Archi 5.10.0: concepts, the "
                "relationship matrix and viewpoints."
            ),
        },
        {
            "name": "Modeling",
            "description": (
                "Language-neutral modeling contract: rulesets and (later) models, "
                "revisions and validation runs for ArchiMate and UML."
            ),
        },
        {
            "name": "UML",
            "description": (
                "UML 2.5.1 initial profile (uml-2.5.1-p1): class, component and sequence "
                "constructs. A stated subset, not full UML."
            ),
        },
        {
            "name": "Architecture Containers",
            "description": (
                "Code-graph containers reachable from a project's effective "
                "architecture (project-scope ∪ tenant-scope) — feeds the "
                "Governance review-queue container selector."
            ),
        },
        # Project-management domain
        {"name": "PM Programmes", "description": "Programme portfolio management."},
        {"name": "PM Projects", "description": "Project management within programmes."},
        {
            "name": "PM Card Ledger",
            "description": "Append-only facts and claims recorded per Planka card.",
        },
        {"name": "PM Resources", "description": "Resource definitions and assignments."},
        {"name": "PM Risks", "description": "Project risk register."},
        {"name": "PM Benefits", "description": "Benefits-realization tracking."},
        {
            "name": "PM Project Phases",
            "description": "Catalogue: the phases a tenant's projects move through.",
        },
        {
            "name": "PM Capabilities",
            "description": "Catalogue: the capabilities involved in projects; each owns card types.",
        },
        {
            "name": "PM Card Types",
            "description": "Catalogue: the kinds of work card, each in one capability.",
        },
        {
            "name": "PM Project Types",
            "description": (
                "Catalogue: project types, their ordered phases and the card types used per "
                "phase; plus the default-catalogue import."
            ),
        },
        {
            "name": "PM Recipes",
            "description": (
                "Catalogue: per project type and phase (and optionally size S/M/L), the card "
                "types to create and how many."
            ),
        },
        {
            "name": "PM WIP Limits",
            "description": (
                "Catalogue: how many cards may be in progress per scope (project type, phase, "
                "capability, card type; null = any), and the most specific limit for a card."
            ),
        },
        {
            "name": "PM Priority Levels",
            "description": (
                "Catalogue: the priorities a card may carry, by rank (0 highest), with one "
                "default and whether the conveyor may set each."
            ),
        },
        {
            "name": "PM Deliverable Kinds",
            "description": "Catalogue: the kinds of thing a project delivers.",
        },
        {
            "name": "PM Milestone Kinds",
            "description": "Catalogue: the milestones a project passes; one may mark a phase's exit.",
        },
        {
            "name": "PM Gates",
            "description": (
                "Catalogue: the human approvals a phase needs before it exits (who approves, "
                "quorum, expiry)."
            ),
        },
        {
            "name": "PM Board Templates",
            "description": (
                "Catalogue: the lists, labels and custom field groups a board starts with."
            ),
        },
        # Cross-domain
        {
            "name": "Integration Tools",
            "description": (
                "Cross-domain integration: workpackage proposals, requirement→architecture tracing."
            ),
        },
    ],
)

# CORS configuration
_default_origins = "http://localhost:8921,http://localhost:3000,http://127.0.0.1:3000"
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", _default_origins).split(",") if o.strip()
]
ALLOW_CREDENTIALS = "*" not in ALLOWED_ORIGINS

# --- Middleware ---
# Starlette executes middleware in reverse-registration order: the last
# add_middleware()/@app.middleware() call is the outermost wrapper and runs
# first on the request path. Target request-side execution order:
#
#   RequestId → Auth → SecurityHeaders → RequestLogging → CORS → handler
#
# Rationale:
#   - RequestId outermost so every downstream layer (incl. auth 401s) can
#     reference request.state.request_id and the X-Request-ID response header
#     is always set.
#   - Auth next so 401s short-circuit before we run the rest of the stack.
#   - SecurityHeaders and RequestLogging wrap CORS + handler so all responses
#     pick up baseline headers and an access-log line.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Accept",
        "Content-Type",
        "Authorization",
        "X-Request-ID",
        "X-Tenant-Id",
    ],
)

# Note: add_request_logging_middleware + add_security_headers_middleware use
# the @app.middleware("http") decorator internally, which registers via the
# same stack as add_middleware(). Registration order below produces the
# reverse-order shown above.
add_request_logging_middleware(app)
add_security_headers_middleware(app)

# Add authentication middleware (conditional on ENABLE_AUTH)
if ENABLE_AUTH:
    app.add_middleware(AuthMiddleware)
else:
    logger.warning("Auth middleware DISABLED (ENABLE_AUTH=false)")

# RequestId outermost — runs first on the request path.
app.add_middleware(RequestIdMiddleware)

# ⛔ NO /metrics ON THE API PORT. Prometheus scrapes a DEDICATED metrics-only
# listener (settings.metrics_port, default 9921) started in lifespan — see
# src/metrics_server.py for why relaxing :8921 for the scrape would expose the API.

register_routers(app)


# --- Error envelope ---
def _error_envelope(
    code: str,
    message: str,
    http_status: int,
    *,
    request_id: str,
    details: str | None = None,
    findings: list[Any] | None = None,
    head: int | None = None,
) -> dict[str, Any]:
    """Build the standardized error envelope.

    Shape matches ``schemas.common.ErrorResponse``: a single ``error`` key with
    ``code``, ``message``, ``status`` (HTTP) plus the request-id and an
    ISO-8601 timestamp. ``details`` is only included when populated.
    """
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "status": http_status,
        "request_id": request_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if details is not None:
        payload["details"] = details
    if findings is not None:
        payload["findings"] = findings
    if head is not None:
        payload["head"] = head
    return {"error": payload}


def _http_exception_to_response(exc: StarletteHTTPException, request_id: str) -> JSONResponse:
    # A structured detail (``{"code": ..., "message": ...}``) carries its own
    # machine-readable code into the envelope. Otherwise fall back to HTTP_<status>.
    # ``model_integrity`` details also carry ``findings`` (services/model_integrity.py),
    # which travel as structured data, never as a stringified dict (spec §6.11.1).
    # ``stale_head`` (412) details carry the model's current ``head`` (spec §7.2).
    detail = exc.detail
    findings = None
    head = None
    if isinstance(detail, dict) and detail.get("code"):
        code = str(detail["code"])
        message = str(detail.get("message") or detail["code"])
        if isinstance(detail.get("findings"), list):
            findings = detail["findings"]
        if isinstance(detail.get("head"), int):
            head = detail["head"]
    else:
        code = f"HTTP_{exc.status_code}"
        message = str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_envelope(
            code, message, exc.status_code, request_id=request_id, findings=findings, head=head
        ),
        headers=exc.headers,
    )


def _find_http_exception(exc: BaseException) -> HTTPException | None:
    """Walk nested ExceptionGroups to find the first HTTPException."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_http_exception(sub)
            if found is not None:
                return found
    return None


# SQLSTATE 57014 `query_canceled` — what Postgres returns when it aborts a
# statement at `statement_timeout` (see `db.register_statement_timeout`). psycopg
# raises it as `psycopg.errors.QueryCanceled`, which SQLAlchemy wraps in
# `OperationalError` with the psycopg exception on `.orig`.
QUERY_CANCELED_SQLSTATE = "57014"


def _find_statement_timeout(exc: BaseException) -> BaseException | None:
    """Walk nested ExceptionGroups to find the first statement-timeout abort.

    Same shape as ``_find_http_exception`` and for the same reason: a handler that
    uses a TaskGroup delivers its failure wrapped, and an unwrapped ``isinstance``
    check would miss it and fall through to a 500.
    """
    if isinstance(exc, OperationalError):
        if getattr(exc.orig, "sqlstate", None) == QUERY_CANCELED_SQLSTATE:
            return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_statement_timeout(sub)
            if found is not None:
                return found
    return None


def _statement_timeout_to_response(exc: BaseException, request_id: str) -> JSONResponse:
    """Render a statement-timeout abort as 504, NOT 500.

    ⛔ THE STATUS CODE IS THE POINT. The AA conveyor retries on 5xx, and a write
    that was aborted at ``statement_timeout`` may or may not have committed —
    "too slow" and "broken" demand different handling, and a bare 500 tells the
    caller nothing that distinguishes them. 504 says the request exceeded its
    time budget at the database, which is what happened.
    """
    logger.warning("db.statement_timeout: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content=_error_envelope(
            "STATEMENT_TIMEOUT",
            "The database query exceeded its time budget",
            status.HTTP_504_GATEWAY_TIMEOUT,
            request_id=request_id,
            details=str(exc) if LEAK_EXCEPTION_DETAILS else None,
        ),
    )


def _request_id_from(request: Request) -> str:
    return request.state.request_id if hasattr(request.state, "request_id") else str(uuid.uuid7())


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render HTTPException (incl. FastAPI's 404) into the envelope."""
    return _http_exception_to_response(exc, _request_id_from(request))


@app.exception_handler(OperationalError)
async def operational_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    """Render a `statement_timeout` abort as 504; re-raise every other DB error.

    Registered on ``OperationalError`` rather than handled only inside the
    catch-all, because Starlette routes the ``Exception`` key to
    ``ServerErrorMiddleware``, which sends the response and then RE-RAISES. That
    is right for an unexpected 500 and wrong for a 504: under
    ``httpx.ASGITransport`` (how this repo's whole test suite and its in-process
    integration suite call the app) the re-raise surfaces as an exception at the
    caller instead of a response. A handler registered on the concrete class is
    served by ``ExceptionMiddleware``, which does not re-raise.

    Anything that is NOT a timeout is re-raised deliberately, so it keeps exactly
    the 500 path — and the ServerErrorMiddleware logging — that it has today.
    """
    timed_out = _find_statement_timeout(exc)
    if timed_out is None:
        raise exc
    return _statement_timeout_to_response(timed_out, _request_id_from(request))


# A caller-supplied extra key that ``extra_forbidden`` reports is shown only when
# it is shaped like a field name. Bounded and charset-limited so the segment can
# carry a misspelt field name (what an agent needs to self-correct) and nothing
# that reads as content: no spaces, punctuation or length to smuggle data through.
_SHOWABLE_EXTRA_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _body_annotation(request: Request) -> Any:
    """The type FastAPI validated this request's body against, or ``None``.

    For a single non-embedded body parameter it is that parameter's own type; for
    an embedded body it is FastAPI's generated ``Body_<op>`` model, whose fields
    are the parameter names — the same shape the ``loc`` has in both cases.
    """
    field = getattr(request.scope.get("route"), "body_field", None)
    return getattr(getattr(field, "field_info", None), "annotation", None)


def _unwrap(tp: Any) -> Any:
    """Strip ``Annotated[...]`` and ``X | None``, which add no ``loc`` segment."""
    while True:
        origin = get_origin(tp)
        if origin is Annotated:
            tp = get_args(tp)[0]
            continue
        if origin in (Union, UnionType):
            members = [a for a in get_args(tp) if a is not type(None)]
            if len(members) == 1:
                tp = members[0]
                continue
        return tp


def _step(tp: Any, segment: Any) -> tuple[bool, Any]:
    """Resolve one ``loc`` segment against ``tp``: ``(is_schema_structure, child_type)``.

    ``is_schema_structure`` is True only when the segment is a name DECLARED in the
    schema (or a sequence index). A mapping key is caller data: False, but the
    walk continues into the mapping's value type so declared names below it are
    still resolved. Anything unrecognised is False with ``None`` — the walk is lost
    and every later string segment is redacted.
    """
    tp = _unwrap(tp)
    origin = get_origin(tp)
    args = get_args(tp)
    if origin is None and isinstance(tp, type) and issubclass(tp, BaseModel):
        if isinstance(segment, str):
            for name, field in tp.model_fields.items():
                if segment == name or segment == field.alias:
                    return True, field.annotation
        return False, None
    if isinstance(segment, int) and not isinstance(segment, bool):
        if origin in (list, set, frozenset) or origin in (Sequence, MutableSequence):
            return True, args[0] if args else None
        if origin is tuple and args:
            if args[-1] is Ellipsis:
                return True, args[0]
            return True, args[segment] if 0 <= segment < len(args) else None
    if origin in (dict, Mapping, MutableMapping):
        return False, args[1] if len(args) == 2 else None
    if origin in (Union, UnionType) and isinstance(segment, str):
        # A smart-union error names the member it tried. Keep it only when it is
        # a declared model's class name; any other tag is redacted.
        for member in args:
            if isinstance(member, type) and issubclass(member, BaseModel):
                if segment == member.__name__:
                    return True, member
    return False, None


def _safe_loc(loc: Iterable[Any], body_type: Any = None, error_type: str | None = None) -> str:
    """Render a pydantic error ``loc`` as a dotted path that carries NO caller data.

    ⛔ WHY THIS IS NOT JUST ``".".join(map(str, loc))``. A ``loc`` is *mostly*
    structural — request location, then declared field names — but it is NOT
    value-free in general, and the exception is real in this codebase:
    ``RequirementIn.links`` is ``dict[str, list[str]]`` (and 36 further
    ``dict[str, …]`` fields exist across ``src/schemas/``), so a CALLER-SUPPLIED
    MAPPING KEY becomes a loc segment — ``("body", "links", "<their key>")``.
    Do not "simplify" this function back into a bare join.

    With ``body_type`` (the model FastAPI validated the body against), a ``body``
    loc is WALKED through the schema, so every segment is classified rather than
    guessed from its depth:

    * an ``int`` segment is a sequence index — positional, not content — kept;
    * the first field name after ``body`` — kept, as in the fallback (no router
      takes a bare mapping body, so it is never a caller key) — UNLESS it is the
      undeclared key of an ``extra_forbidden``, which follows the next-but-one rule;
    * a name DECLARED on the model at that point in the walk — kept, at any depth
      (``body.elements.0.attributes.properties.0.id``);
    * a MAPPING KEY (any ``dict``/``Mapping`` field) — ``*``, always; the walk
      continues into the value type, so ``body.links.*.0`` still names the index;
    * the undeclared key of an ``extra_forbidden`` error, as the LAST segment, when
      it is identifier-shaped (``_SHOWABLE_EXTRA_KEY``) — kept: it is the caller's
      misspelt field name, which is exactly what they need to fix the request.
      This holds at every depth, the top level and the depth fallback included.
      Any other undeclared or unresolvable segment — ``*``.

    Any exception raised by the walk falls back to the depth rule: a 422 must
    never become a 500.

    Without ``body_type`` (and for non-``body`` locations) the older depth rule
    applies: location and the first field name are kept, deeper strings are ``*``.
    That over-redacts, which is the safe direction for a request we cannot walk.

    Values never enter: only ``loc`` is read, and ``input``/``ctx``/``msg`` stay out.
    """
    segments = list(loc)
    if not segments:
        return "unknown"
    try:
        return _walked_loc(segments, body_type, error_type)
    except Exception:  # noqa: BLE001 — a 422 must never become a 500
        # Anything the walk did not anticipate (an odd annotation, a pydantic
        # internals change) degrades to the over-redacting depth rule.
        return _walked_loc(segments, None, error_type)


def _is_extra_key(segments: list[Any], index: int, error_type: str | None) -> bool:
    """True when ``segments[index]`` is the undeclared key an ``extra_forbidden`` names."""
    return error_type == "extra_forbidden" and index == len(segments) - 1


def _walked_loc(segments: list[Any], body_type: Any, error_type: str | None) -> str:
    """The body of :func:`_safe_loc`; ``body_type=None`` is the pure depth rule.

    ⛔ The undeclared key of an ``extra_forbidden`` error is caller-chosen text at
    EVERY depth, the top level included (62 ``extra="forbid"`` models), so it is
    shown only when ``_SHOWABLE_EXTRA_KEY`` matches — checked before any rule
    that would otherwise keep the segment unconditionally.
    """
    parts: list[str] = []
    walking = segments[0] == "body" and body_type is not None
    tp: Any = body_type
    named = 0
    for index, segment in enumerate(segments):
        is_index = isinstance(segment, int) and not isinstance(segment, bool)
        if index == 0:
            parts.append(str(segment))
            named += 0 if is_index else 1
            continue
        if is_index:
            parts.append(str(segment))
            if walking and tp is not None:
                _, tp = _step(tp, segment)
            continue
        named += 1
        if not walking:
            if _is_extra_key(segments, index, error_type):
                shown = named <= 2 and _SHOWABLE_EXTRA_KEY.match(str(segment))
                parts.append(str(segment) if shown else "*")
            else:
                parts.append(str(segment) if named <= 2 else "*")
            continue
        parent = _unwrap(tp)
        declared, tp = _step(tp, segment) if tp is not None else (False, None)
        if declared:
            parts.append(str(segment))
        elif _is_extra_key(segments, index, error_type):
            shown = (
                isinstance(segment, str)
                and (named == 2 or (isinstance(parent, type) and issubclass(parent, BaseModel)))
                and _SHOWABLE_EXTRA_KEY.match(segment)
            )
            parts.append(segment if shown else "*")
        elif named == 2:
            # Keeps the fallback's guarantee for the first field name: no router
            # takes a bare mapping body, so it is never a key — and a handler that
            # raises its own ``RequestValidationError`` (e.g.
            # ``body.brief:brief_over_128_kib``) has a loc the route's body model
            # does not declare.
            parts.append(str(segment))
        else:
            parts.append("*")
    return ".".join(parts)


# The ceiling on rendered ``loc:type`` pairs in a 422 ``details``.
#
# ⛔ WHY A CAP EXISTS AT ALL. One pair is rendered PER PYDANTIC ERROR, and the
# CALLER controls how many errors there are, so without this bound ``details`` —
# and therefore the whole 422 response — is proportional to caller input, where
# every other error path in this service is constant-size.
#
# Measured on this branch with the cap reverted, driving the real app: a body
# with a 5,000-key ``links`` mapping produced a **120,186 B response** (119,998 B
# of it ``details``). ⚠️ The figure that matters is that response size, NOT a
# ratio: the response tracks the ERROR COUNT alone, while the request can be made
# arbitrarily compact for the same count, so the amplification ratio is whatever
# the caller's key-length choice makes it (0.96× against a 125,060 B request of
# long keys here; the audit that found this measured 2.23× against a 53,950 B
# request of short ones, and shorter keys would push it higher still). An error
# envelope must not be an amplifier. With the cap the same request answers in
# **804 B** — and that figure is invariant to key length, which is what shows the
# ratio was always the caller's choice rather than a property of the handler.
# ⚠️ But the bound is PER-ENDPOINT, not constant, and an earlier draft of this
# comment overclaimed it as "no request can make it larger". Measured refutation:
# an 839 B POST to /pm/activities with all 29 ``ActivityIn`` fields wrong-typed
# answers in 1,000 B (812 B of it ``details``, truncated at ``(+4 more)``) — ratio
# 1.19. Index segments also add O(cap x log10(request size)), i.e. ~175 B per extra
# digit-decade, which needs ~36 MB of request to buy seven digits: nonzero, not
# negligible-by-assumption. ⇒ The true claim is that the response is no longer
# PROPORTIONAL to caller input and is bounded at ~1 KB per endpoint today. Do not
# restate it as constant-size.
#
# ⛔ WHY 25, and why it is not an arbitrary round number. Truncation must
# essentially never fire on a legitimate SINGLE-OBJECT request, because naming
# the field of such a request is the entire reason this handler was ungated.
# Measured over ``src/schemas/`` (99 ``BaseModel`` subclasses), counting the models
# actually annotated as a router handler parameter — 52 of them; ⚠️ an earlier
# draft of this comment said "29 request-shaped models", which reproduces under NO
# definition and was a count without its predicate: 51 of those 52 declare 25
# fields or fewer — only ``pm.ActivityIn``, at 29, was wider (deleted with
# the schedule apparatus, R352) — so a caller who
# gets EVERY field of one object wrong still gets every field named. What 25 does
# truncate is the multi-item body and the caller-keyed mapping blow-up, which is
# exactly the amplification measured above.
#
# ⚠️ Do not lower this to a "tidier" 10. A diagnostician needs the first errors,
# and at 10 a measured 14 of those 52 models could no longer be fully named, so
# truncation
# would become the COMMON case for ordinary single-object 422s.
_MAX_VALIDATION_LOC_TYPES = 25


def _validation_loc_types(exc: RequestValidationError, body_type: Any = None) -> str:
    """Name WHICH field failed and HOW, without echoing WHAT was sent.

    ⛔ UNGATED BY DESIGN, and that is the point of the change. ``details`` used to
    sit behind ``LEAK_EXCEPTION_DETAILS``, which is OFF in production, so a 422
    named no field at all — 15 AA conveyor cards blocked on
    ``createArchitectureElement``: ``422 - Request validation failed`` and four
    sittings spent guessing which field it was.

    ⛔ ``LEAK_EXCEPTION_DETAILS`` IS NOT WIDENED AND MUST NOT BE. Only ``loc``
    (redacted per ``_safe_loc``) and ``type`` are emitted. The other keys of a
    pydantic error are caller or server data and stay out: ``input`` is the
    caller's own payload, ``ctx`` for a ``literal_error`` ENUMERATES THE WHOLE
    PERMITTED VOCABULARY, ``msg`` interpolates the input, and ``url`` is a
    pydantic docs link. ``type`` is a fixed pydantic tag (``missing``,
    ``string_too_long``, ``literal_error``) with no interpolation.

    BOUNDED at ``_MAX_VALIDATION_LOC_TYPES`` pairs — see that constant for why.
    The pairs KEPT are the first ones ``exc.errors()`` reports, in pydantic's own
    order: a non-deterministic subset would be useless for diagnosis and
    impossible to write a failing test against.
    """
    errors = list(exc.errors())
    rendered = [
        f"{_safe_loc(error.get('loc', ()), body_type, error.get('type'))}:"
        f"{error.get('type', 'unknown')}"
        for error in errors[:_MAX_VALIDATION_LOC_TYPES]
    ]
    if not rendered:
        return "unknown"
    joined = ", ".join(rendered)
    omitted = len(errors) - len(rendered)
    if omitted:
        # ⛔ The marker carries a COUNT and nothing else. It is deliberately
        # explicit about the omission so a reader can never mistake a truncated
        # list for a complete one, and deliberately ASCII: this string is read
        # from logs and terminals, where a non-ASCII ellipsis can arrive as
        # mojibake that a reader could mistake for caller content.
        joined = f"{joined}, ... (+{omitted} more)"
    return joined


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render pydantic request-validation errors into the envelope (HTTP 422)."""
    request_id = _request_id_from(request)
    # A schema-level model-integrity refusal (schemas.modeling.integrity_value_error)
    # carries a finding; render it as the same ``model_integrity`` body the
    # service-level checks use, so callers see one shape however a rule fired.
    findings = [
        e["ctx"]["finding"]
        for e in exc.errors()
        if e.get("type") == INTEGRITY_ERROR_TYPE and isinstance(e.get("ctx"), dict)
    ]
    if findings:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_envelope(
                INTEGRITY_ERROR_TYPE,
                f"{findings[0]['rule_id']}: {findings[0]['message']}",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                request_id=request_id,
                findings=findings,
            ),
        )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=_error_envelope(
            "VALIDATION_ERROR",
            "Request validation failed",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            request_id=request_id,
            details=_validation_loc_types(exc, _body_annotation(request)),
        ),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for uncaught exceptions; returns the standardized envelope."""
    request_id = _request_id_from(request)

    http_exc = _find_http_exception(exc)
    if http_exc is not None:
        return _http_exception_to_response(http_exc, request_id)

    # The GROUP-WRAPPED timeout. A bare OperationalError is claimed by the
    # handler above; one raised inside a TaskGroup arrives here as an
    # ExceptionGroup, which matches no concrete handler.
    timed_out = _find_statement_timeout(exc)
    if timed_out is not None:
        return _statement_timeout_to_response(timed_out, request_id)

    logger.error("unhandled_error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_envelope(
            "INTERNAL_SERVER_ERROR",
            "An unexpected error occurred",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            request_id=request_id,
            details=str(exc) if LEAK_EXCEPTION_DETAILS else None,
        ),
    )


# Add Bearer security scheme so Swagger UI shows Authorize
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    components = openapi_schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes.setdefault(
        "BearerAuth",
        {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "License",
            "description": (
                "License token. Transport: `X-Tenant-Id: Bearer <token>` "
                "(introspected against LICENSE_INTROSPECT_URL)."
            ),
        },
    )

    parameters = components.setdefault("parameters", {})
    parameters.setdefault(
        "TenantIdHeader",
        {
            "name": "X-Tenant-Id",
            "in": "header",
            "required": False,
            "description": (
                "`Bearer <licence token>`, or — when the licence is sent in "
                "`Authorization: Bearer` — the licence's own tenant (UUID or NAME, "
                "resolved against the licences registry). Every route except the "
                "health probes and /openapi.json requires a licence."
            ),
            "schema": {"type": "string"},
        },
    )

    for path, path_item in openapi_schema.get("paths", {}).items():
        normalized = path.rstrip("/") or "/"
        is_public = is_public_path(normalized)

        for operation in list(path_item.values()):
            if not isinstance(operation, dict):
                continue

            if is_public:
                operation.pop("security", None)
            else:
                operation.setdefault("security", [{"BearerAuth": []}])

            op_params = operation.setdefault("parameters", [])
            has_tenant = any(
                p.get("name", "").lower() == "x-tenant-id" and p.get("in") == "header"
                for p in op_params
            )
            if not has_tenant:
                op_params.append({"$ref": "#/components/parameters/TenantIdHeader"})
    app.openapi_schema = openapi_schema
    return app.openapi_schema


object.__setattr__(
    app, "openapi", custom_openapi
)  # FastAPI standard pattern: replace method with function


@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint - redirects to API documentation."""
    return RedirectResponse(url="/docs")

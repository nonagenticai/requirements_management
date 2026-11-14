"""Common OpenAPI response definitions for authenticated endpoints."""

from collections.abc import Callable
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute

from schemas.common import ErrorResponse

# Standard 401 response for endpoints requiring authentication
AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Authentication required", "model": ErrorResponse},
}

# Standard 404 response for endpoints that may not find resources
NOT_FOUND_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Resource not found", "model": ErrorResponse},
}

# Combined responses for authenticated endpoints that access specific resources
AUTH_AND_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    **NOT_FOUND_RESPONSES,
}

# Access forbidden response
FORBIDDEN_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Access forbidden", "model": ErrorResponse},
}

# Combined auth, forbidden, and not found
AUTH_FORBIDDEN_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    **FORBIDDEN_RESPONSES,
    **NOT_FOUND_RESPONSES,
}


# ---------------------------------------------------------------------------
# Route class: the error envelope on every documented error (client generation)
# ---------------------------------------------------------------------------

INVALID_IF_MATCH = "`invalid_if_match`: If-Match is not a model head"
VALIDATION_422_DESCRIPTION = "`VALIDATION_ERROR`: the request does not validate"


def _inputs(path: str, endpoint: Any) -> list[Any]:
    """Every request input (path/query/header/cookie/body field) of the route's tree."""
    out: list[Any] = []
    stack: list[Dependant] = [get_dependant(path=path, call=endpoint)]
    while stack:
        d = stack.pop()
        out += d.path_params + d.query_params + d.header_params + d.cookie_params
        out += d.body_params
        stack.extend(d.dependencies)
    return out


def error_envelope_responses(
    path: str, endpoint: Any, responses: dict[int | str, dict[str, Any]] | None
) -> dict[int | str, dict[str, Any]]:
    """``responses`` as the wire really answers: every 4xx/5xx entry without a model
    gets ``ErrorResponse`` (the canonical envelope — on a 412 it carries ``head``); a
    route with inputs documents its 422 with the envelope (FastAPI would document
    ``HTTPValidationError``, a shape this service never sends); a route that accepts
    ``If-Match`` names ``invalid_if_match`` on that 422."""
    inputs = _inputs(path, endpoint)
    out: dict[int | str, dict[str, Any]] = {}
    for code, spec in (responses or {}).items():
        key: int | str = int(code) if str(code).isdigit() else code
        if str(key)[0] in "45" and "model" not in spec and "content" not in spec:
            spec = {**spec, "model": ErrorResponse}
        out[key] = spec
    if inputs and 422 not in out:
        out[422] = {"description": VALIDATION_422_DESCRIPTION, "model": ErrorResponse}
    if any(getattr(f, "alias", None) == "If-Match" for f in inputs):
        r422 = out.get(422) or {"description": VALIDATION_422_DESCRIPTION}
        description = r422.get("description", "")
        if "invalid_if_match" not in description:
            description = f"{description}; {INVALID_IF_MATCH}" if description else INVALID_IF_MATCH
        out[422] = {**r422, "description": description, "model": r422.get("model", ErrorResponse)}
    return out


class ErrorEnvelopeRoute(APIRoute):
    """An ``APIRoute`` whose OpenAPI error responses are :func:`error_envelope_responses`."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        kwargs["responses"] = error_envelope_responses(path, endpoint, kwargs.get("responses"))
        super().__init__(path, endpoint, **kwargs)

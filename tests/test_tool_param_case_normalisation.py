"""``schemas.tools``' Literal fields must coerce case the way their twins do.

Why this file exists — measured, not hypothetical
-------------------------------------------------
``src/schemas/tools.py`` had **zero** ``field_validator``s before the closed
vocabularies were published, so its new ``Literal``s arrived with no
``mode="before"`` normaliser while the *same* vocabularies in
``schemas.architecture`` coerce via ``lower_strip``. That made one vocabulary
disagree with itself about case:

    POST /architecture/views              {"layer": "Business"}    -> accepted
    POST /architecture/tools/generate-view {"viewpoint": "Layered"} -> 422

``model_config = ConfigDict(str_strip_whitespace=True)`` does NOT close the gap:
it is a property of the *str* core schema and never reaches a ``Literal`` field.

The dropdown the OpenAPI ``enum`` produces only protects hand-authored values.
The AA conveyor's *Dispatch Requirements* node forwards
``$json.step.parameters.*`` verbatim, so a mis-cased LLM-authored value fails at
RUNTIME rather than at author time — which is why this is a defect and not a
style preference.

Structure — deliberately ONE test per field
-------------------------------------------
Each field gets its own independent test so that the killing mutation (delete
the normaliser from a single field) turns exactly one test RED. A single
parametrised test over all six would let one fix cover another.

Each test asserts three things, because the first alone passes vacuously:
  1. the canonical lower-case value still round-trips  (kills "reject everything")
  2. a title-cased / padded value is ACCEPTED and COERCED  (the actual fix)
  3. a genuinely invalid value still raises  (kills "accept everything")
"""

import pytest
from pydantic import ValidationError

from schemas.tools import (
    TraceabilityMatrixRequest,
    ViewGenerationRequest,
)

PROJECT = {"project_id": "p1"}


def _roundtrip(model, field, canonical, variant, invalid, **extra):
    """Assert canonical survives, variant coerces to canonical, invalid raises."""
    assert getattr(model(**PROJECT, **extra, **{field: canonical}), field) == canonical, (
        f"{model.__name__}.{field}: the canonical value must still be accepted — "
        "without this leg a field that rejects everything would pass"
    )
    assert getattr(model(**PROJECT, **extra, **{field: variant}), field) == canonical, (
        f"{model.__name__}.{field}: {variant!r} must be coerced to {canonical!r}. "
        "Its twin in schemas.architecture accepts the same spelling; one "
        "vocabulary must not disagree with itself about case."
    )
    with pytest.raises(ValidationError):
        model(**PROJECT, **extra, **{field: invalid})


def test_traceability_root_layer_coerces_case():
    _roundtrip(TraceabilityMatrixRequest, "root_layer", "business", " Business ", "Busines")


def test_view_generation_viewpoint_coerces_case():
    _roundtrip(
        ViewGenerationRequest,
        "viewpoint",
        "application_cooperation",
        "  Application_Cooperation ",
        "application-cooperation",  # the hyphen spelling a test fixture once carried
    )


def test_non_string_input_still_reports_a_type_error():
    """The normaliser passes non-str through so pydantic reports the real error.

    A normaliser that stringified its input would turn a client bug into a
    silently-accepted value.
    """
    with pytest.raises(ValidationError):
        TraceabilityMatrixRequest(**PROJECT, root_layer=7)  # ty: ignore[invalid-argument-type]


def test_every_literal_field_in_tools_has_a_normaliser():
    """A new closed vocabulary added here without a normaliser must fail CI.

    This is the ratchet: the defect was not that one field was missed, it was
    that the module had no convention at all.
    """
    import inspect
    import typing

    import schemas.tools as tools

    missing = []
    for name, obj in vars(tools).items():
        if not (inspect.isclass(obj) and issubclass(obj, tools.BaseModel)):
            continue
        if obj.__module__ != tools.__name__:
            continue
        for fname, finfo in obj.model_fields.items():
            ann = finfo.annotation
            args = typing.get_args(ann)
            is_literal = typing.get_origin(ann) is typing.Literal or any(
                typing.get_origin(a) is typing.Literal for a in args
            )
            if not is_literal:
                continue
            decorators = obj.__pydantic_decorators__.field_validators
            covered = any(
                fname in d.info.fields and d.info.mode == "before" for d in decorators.values()
            )
            if not covered:
                missing.append(f"{obj.__name__}.{fname}")

    assert not missing, (
        "these schemas.tools Literal fields have no mode='before' normaliser, so "
        f"they will reject a title-cased value their architecture.py twin accepts: {missing}"
    )

"""API shapes of exchange-format interchange and view rendering (target spec §8.1)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from schemas.modeling import ArchimateOrganization
from schemas.modeling_lifecycle import ChangeSetOut


class ExchangeLossOut(BaseModel):
    """One construct that did not survive the transform (stable ``code``)."""

    code: str = Field(..., description="Stable loss code, e.g. ID-REWRITTEN, ORGANIZATION")
    id: str | None = Field(None, description="Identifier of the affected construct, if any")
    xpath: str | None = Field(None, description="Location in the exchange document, if any")
    message: str


class ExchangeExportReport(BaseModel):
    """``?report=true`` form of an export: the document and every loss."""

    xml: str = Field(..., description="The exchange document (UTF-8 XML)")
    losses: list[ExchangeLossOut]


class ExchangeImportOut(BaseModel):
    """A staged import: the change set it produced plus the import's own report.

    Apply the change set with ``POST /modeling/change-sets/{change_set_id}/apply``; the
    import itself writes nothing to the model. The same report is stored on the change
    set as findings of ruleset ``modeling/exchange-import`` (``XI-META-001`` carries the
    metadata, ``XI-LOSS-001`` one loss each).
    """

    model_id: UUID
    model_created: bool = Field(..., description="True when the import created the model")
    change_set: ChangeSetOut
    source_sha256: str = Field(..., description="sha256 of the uploaded bytes")
    source_bytes: int
    source_filename: str | None
    schema_valid: bool = Field(..., description="Document validated against the pinned 3.1 XSDs")
    losses: list[ExchangeLossOut]
    id_resolution: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Imported id -> the existing row id it updates, or the create op's local "
            "reference when that differs from the imported id (collision)"
        ),
    )


class RenderReport(BaseModel):
    """``?report=true`` form of a render: the sanitized SVG and its diagnostics."""

    svg: str
    diagnostics: list[dict[str, Any]]
    renderer_version: str


class OrganizationsIn(BaseModel):
    """The model's whole folder tree; ``null`` = none (export writes Archi's defaults)."""

    model_config = ConfigDict(extra="forbid")

    organizations: list[ArchimateOrganization] | None = Field(
        ...,
        description=(
            "Folders [{id?, name?, documentation?, items[], children[]}]; items are ids of "
            "the model's concepts, relationships and views"
        ),
    )


class OrganizationsOut(BaseModel):
    model_id: UUID
    head: int = Field(..., description="The model's draft head (also the ETag)")
    organizations: list[ArchimateOrganization] | None

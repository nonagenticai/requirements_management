"""ORM models package for the requirements management application."""

from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper

from models.architecture import (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    Standard,
)
from models.decisions import Decision
from models.idempotency import IdempotencyKey
from models.modeling import (
    ArchitectureViewConnection,
    ArchitectureViewObject,
    ModelingChangeSet,
    ModelingModel,
    ModelingModelRevision,
    ModelingPublication,
    ModelingReview,
    ModelingTraceLink,
    ModelingValidationFinding,
    ModelingValidationRun,
)
from models.pm import (
    Benefit,
    Programme,
    Project,
    Resource,
    Risk,
)
from models.pm_catalogue import (
    BoardTemplate,
    Capability,
    CardType,
    DeliverableKind,
    Gate,
    MilestoneKind,
    PriorityLevel,
    ProjectPhase,
    ProjectType,
    Recipe,
    WipLimit,
)
from models.principles import Principle
from models.requirements import Requirement
from models.toc import TocNode, TocSettings, TocTree
from models.uml import (
    UmlDiagram,
    UmlDiagramConnection,
    UmlDiagramObject,
    UmlElement,
    UmlInteraction,
    UmlRelationship,
)
from models.version_history import EntityVersionHistory


def _autofill_subject_id(mapper: Mapper[Any], connection: Connection, target: Any) -> None:
    """Auto-fill subject_id := tenant_id when caller didn't set one.

    Also auto-fills tenant_name := tenant_id when the caller didn't set it.

    ⚠️ Since Phase 4 (``2026_08_24_0042``) ``tenant_id`` is a ``uuid`` column, so
    this fallback now writes a UUID into a column whose job is to be
    human-readable. That is a degraded label, not a wrong one — the row is still
    correctly attributed, and every row that existed before the flip kept its
    real label because ``…0019`` backfilled it first. A caller that wants a
    readable ``tenant_name`` must pass one; the only place that knows it is the
    request boundary (``X-Tenant-Name``, see ``auth/tenant.py``), and wiring it
    through is deliberately not part of the type flip.

    Eliminates the duplicated ``subject_id or tenant_id`` fallback that would
    otherwise live in every service write site. Matches discovery_engine
    Phase 5 + the canonical isolation standard (2026-04-27).
    """
    if not getattr(target, "subject_id", None):
        target.subject_id = target.tenant_id
    if not getattr(target, "tenant_name", None):
        target.tenant_name = target.tenant_id


# Register on every owned model.
for _model in (
    Requirement,
    Principle,
    Decision,
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    Standard,
    Programme,
    Project,
    Resource,
    Risk,
    Benefit,
    TocTree,
    TocNode,
    ProjectPhase,
    Capability,
    CardType,
    ProjectType,
    Recipe,
    WipLimit,
    PriorityLevel,
    DeliverableKind,
    MilestoneKind,
    Gate,
    BoardTemplate,
):
    event.listen(_model, "before_insert", _autofill_subject_id)


__all__ = [
    "ArchitectureElement",
    "ArchitectureRelationship",
    "ArchitectureView",
    "ArchitectureViewConnection",
    "ArchitectureViewObject",
    "Benefit",
    "BoardTemplate",
    "Capability",
    "CardType",
    "Decision",
    "DeliverableKind",
    "EntityVersionHistory",
    "IdempotencyKey",
    "Gate",
    "MilestoneKind",
    "ModelingChangeSet",
    "ModelingModel",
    "ModelingModelRevision",
    "ModelingPublication",
    "ModelingReview",
    "ModelingTraceLink",
    "ModelingValidationFinding",
    "ModelingValidationRun",
    "PriorityLevel",
    "Principle",
    "Programme",
    "Project",
    "ProjectPhase",
    "ProjectType",
    "Recipe",
    "Requirement",
    "Resource",
    "Risk",
    "Standard",
    "TocNode",
    "TocSettings",
    "TocTree",
    "UmlDiagram",
    "UmlDiagramConnection",
    "UmlDiagramObject",
    "UmlElement",
    "UmlInteraction",
    "UmlRelationship",
    "WipLimit",
]

"""Service layer — business logic between routers and database."""

from models.architecture import (
    ArchitectureElement,
    ArchitectureRelationship,
    ArchitectureView,
    Standard,
)
from models.decisions import Decision
from models.pm import (
    Benefit,
    Programme,
    Project,
    Resource,
    Risk,
)
from models.principles import Principle
from models.requirements import Requirement
from models.toc import TocNode, TocTree
from schemas.architecture import (
    ArchitectureElementOut,
    ArchitectureRelationshipOut,
    ArchitectureViewOut,
    StandardOut,
)
from schemas.decisions import DecisionIn, DecisionOut
from schemas.pm import (
    BenefitOut,
    ProgrammeOut,
    ProjectOut,
    ResourceOut,
    RiskOut,
)
from schemas.principles import PrincipleIn, PrincipleOut
from schemas.requirements import RequirementIn, RequirementOut
from schemas.toc import TocNodeOut, TocTreeOut
from services.entity_service import EntityService
from services.governed_asset_service import GovernedAssetService
from services.pm_project_phase_service import ProjectService
from services.relationship_service import RelationshipService
from services.search_service import SearchService
from services.version_service import VersionService
from services.view_occurrences import ArchitectureViewService

# Register fields (REG-1, migration 0067) every register entity's history carries.
REGISTER_SNAPSHOT_FIELDS = ("phase", "card_id", "raised_at")

requirement_service = GovernedAssetService(
    model_class=Requirement,
    out_schema=RequirementOut,
    entity_type="requirement",
    in_schema=RequirementIn,
    snapshot_fields=(
        "title",
        "description",
        "project_id",
        "links",
        "acceptance",
        "effort",
        "reach",
        "impact",
        "confidence",
        "version",
        "version_status",
        "version_notes",
        "approved_by",
        "approved_at",
        "published_at",
        "supersedes",
        "superseded_by",
        *REGISTER_SNAPSHOT_FIELDS,
        "status",
        "owner",
    ),
)

principle_service = GovernedAssetService(
    model_class=Principle,
    out_schema=PrincipleOut,
    entity_type="principle",
    in_schema=PrincipleIn,
    snapshot_fields=("title", "statement", "rationale", "implications", "tags"),
)

decision_service = GovernedAssetService(
    model_class=Decision,
    out_schema=DecisionOut,
    entity_type="decision",
    in_schema=DecisionIn,
    snapshot_fields=(
        "title",
        "context",
        "decision",
        "consequences",
        "alternatives",
        "status",
        *REGISTER_SNAPSHOT_FIELDS,
        "decided_by_kind",
        "decided_at",
    ),
)

# Approval / publication of modeling content goes through review + publish of a sealed
# revision (services/model_reviews.py); legacy writes cannot set these row fields.
MODELING_APPROVAL_FIELDS = frozenset({"approved_by", "approved_at", "published_at"})

architecture_element_service = EntityService(
    model_class=ArchitectureElement,
    out_schema=ArchitectureElementOut,
    entity_type="architecture_element",
    snapshot_fields=(
        "title",
        "description",
        "layer",
        "element_type",
        "status",
        "architecture_state",
        "properties",
    ),
    extra_server_managed=MODELING_APPROVAL_FIELDS,
    ignored_on_create=MODELING_APPROVAL_FIELDS,
)

architecture_relationship_service = RelationshipService(
    model_class=ArchitectureRelationship,
    out_schema=ArchitectureRelationshipOut,
)

architecture_view_service = ArchitectureViewService(
    model_class=ArchitectureView,
    out_schema=ArchitectureViewOut,
    entity_type="architecture_view",
    snapshot_fields=(
        "title",
        "description",
        "viewpoint",
        "element_ids",
        "relationship_ids",
        "layout_data",
    ),
    extra_server_managed=MODELING_APPROVAL_FIELDS,
    ignored_on_create=MODELING_APPROVAL_FIELDS,
)

architecture_standard_service = EntityService(
    model_class=Standard,
    out_schema=StandardOut,
    entity_type="architecture_standard",
    snapshot_fields=(
        "title",
        "description",
        "standard_type",
        "status",
        "compliance_level",
        "related_principle_ids",
    ),
)

programme_service = EntityService(
    model_class=Programme,
    out_schema=ProgrammeOut,
    entity_type="programme",
    snapshot_fields=("title", "description", "status", "start_date", "end_date", "blueprint_data"),
)

project_service = ProjectService(
    model_class=Project,
    out_schema=ProjectOut,
    entity_type="project",
    snapshot_fields=(
        "title",
        "description",
        "programme_id",
        "tranche",
        "status",
        "start_date",
        "end_date",
        "budget",
        "currency",
        "budget_currency",
    ),
)

resource_service = EntityService(
    model_class=Resource,
    out_schema=ResourceOut,
    entity_type="resource",
    snapshot_fields=(
        "title",
        "description",
        "resource_type",
        "capacity_per_day",
        "cost_per_unit",
        "skills",
    ),
)

risk_service = EntityService(
    model_class=Risk,
    out_schema=RiskOut,
    entity_type="risk",
    snapshot_fields=(
        "title",
        "description",
        "project_id",
        "probability",
        "impact_score",
        "risk_score",
        "category",
        "response_strategy",
        "response_plan",
        "status",
        "owner",
        *REGISTER_SNAPSHOT_FIELDS,
        "due_at",
        "last_reviewed_at",
    ),
)

benefit_service = EntityService(
    model_class=Benefit,
    out_schema=BenefitOut,
    entity_type="benefit",
    snapshot_fields=(
        "title",
        "description",
        "programme_id",
        "benefit_type",
        "measurement",
        "baseline_value",
        "target_value",
        "actual_value",
        "status",
        "owner",
    ),
)

toc_tree_service = EntityService(
    model_class=TocTree,
    out_schema=TocTreeOut,
    entity_type="toc_tree",
    snapshot_fields=(
        "title",
        "description",
        "tree_type",
        "version",
        "version_status",
        "version_notes",
        "approved_by",
        "approved_at",
        "published_at",
        "supersedes",
        "superseded_by",
    ),
)

toc_node_service = EntityService(
    model_class=TocNode,
    out_schema=TocNodeOut,
    entity_type="toc_node",
    snapshot_fields=("title", "statement", "node_kind", "tree_id", "layout"),
)

version_service = VersionService()
search_service = SearchService()

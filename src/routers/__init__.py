"""Router package with registration helper."""

from fastapi import FastAPI

from routers.architecture_containers import router as arch_containers_router
from routers.architecture_elements import router as arch_elements_router
from routers.architecture_metamodel import metamodel_router, rulesets_router, viewpoints_router
from routers.architecture_relationships import router as arch_relationships_router
from routers.architecture_standards import router as arch_standards_router
from routers.architecture_tools import router as arch_tools_router
from routers.architecture_views import router as arch_views_router
from routers.bulk import router as bulk_router
from routers.decisions import router as decisions_router
from routers.health import router as health_router
from routers.integration_tools import router as integration_tools_router
from routers.modeling_document_export import router as modeling_document_export_router
from routers.modeling_exchange import exchange_router as modeling_exchange_router
from routers.modeling_exchange import view_render_router as arch_view_render_router
from routers.modeling_models import change_sets_router as modeling_change_sets_router
from routers.modeling_models import router as modeling_models_router
from routers.modeling_models import trace_links_router as modeling_trace_links_router
from routers.modeling_models import validation_runs_router as modeling_runs_router
from routers.pm_benefits import router as pm_benefits_router
from routers.pm_card_ledger import router as pm_card_ledger_router
from routers.pm_catalogue import (
    board_templates_router as pm_board_templates_router,
)
from routers.pm_catalogue import (
    capabilities_router as pm_capabilities_router,
)
from routers.pm_catalogue import (
    card_types_router as pm_card_types_router,
)
from routers.pm_catalogue import (
    catalogue_router as pm_catalogue_router,
)
from routers.pm_catalogue import (
    deliverable_kinds_router as pm_deliverable_kinds_router,
)
from routers.pm_catalogue import (
    gates_router as pm_gates_router,
)
from routers.pm_catalogue import (
    milestone_kinds_router as pm_milestone_kinds_router,
)
from routers.pm_catalogue import (
    priority_levels_router as pm_priority_levels_router,
)
from routers.pm_catalogue import (
    project_phases_router as pm_project_phases_router,
)
from routers.pm_catalogue import (
    project_types_router as pm_project_types_router,
)
from routers.pm_catalogue import (
    recipes_router as pm_recipes_router,
)
from routers.pm_catalogue import (
    wip_limits_router as pm_wip_limits_router,
)
from routers.pm_programmes import router as pm_programmes_router
from routers.pm_projects import router as pm_projects_router
from routers.pm_resources import router as pm_resources_router
from routers.pm_risks import router as pm_risks_router
from routers.principle_conflicts import router as principle_conflicts_router
from routers.principles import router as principles_router
from routers.requirements import router as requirements_router
from routers.search import router as search_router
from routers.toc import router as toc_router
from routers.uml_models import revision_render_router as uml_revision_render_router
from routers.uml_models import router as uml_models_router
from routers.uml_tools import router as uml_tools_router


def register_routers(app: FastAPI) -> None:
    """Register all routers on the FastAPI application.

    Static/tool routes registered before wildcard CRUD routes.
    """
    app.include_router(health_router)
    # Static paths first (before wildcard routes)
    app.include_router(bulk_router, tags=["Bulk Operations"])
    app.include_router(search_router, tags=["Search"])
    # Entity routers with wildcard paths
    app.include_router(requirements_router, prefix="/requirements", tags=["Requirements"])
    app.include_router(principles_router, prefix="/principles", tags=["Principles"])
    app.include_router(
        principle_conflicts_router,
        prefix="/principle-conflicts",
        tags=["Principle Conflicts"],
    )
    app.include_router(decisions_router, prefix="/decisions", tags=["Decisions"])
    app.include_router(toc_router, prefix="/toc", tags=["Logical Thinking Process"])
    app.include_router(
        arch_elements_router,
        prefix="/architecture/elements",
        tags=["Architecture Elements"],
    )
    app.include_router(
        arch_relationships_router,
        prefix="/architecture/relationships",
        tags=["Architecture Relationships"],
    )
    app.include_router(
        arch_views_router,
        prefix="/architecture/views",
        tags=["Architecture Views"],
    )
    app.include_router(
        arch_view_render_router,
        prefix="/architecture/views",
        tags=["Architecture Views"],
    )
    app.include_router(
        arch_standards_router,
        prefix="/architecture/standards",
        tags=["Architecture Standards"],
    )
    # Architecture tools — registered before wildcard CRUD routes
    app.include_router(
        arch_tools_router,
        prefix="/architecture/tools",
        tags=["Architecture Tools"],
    )
    # ArchiMate reference data (Archi-derived registry) + the rule catalogue.
    app.include_router(
        metamodel_router, prefix="/architecture/metamodel", tags=["Architecture Metamodel"]
    )
    app.include_router(
        viewpoints_router, prefix="/architecture/viewpoints", tags=["Architecture Metamodel"]
    )
    app.include_router(rulesets_router, prefix="/modeling/rulesets", tags=["Modeling"])
    # Before the model CRUD router: POST /modeling/models/exchange-imports is a static path.
    app.include_router(modeling_exchange_router, prefix="/modeling/models", tags=["Modeling"])
    app.include_router(
        modeling_document_export_router, prefix="/modeling/models", tags=["Modeling"]
    )
    app.include_router(modeling_models_router, prefix="/modeling/models", tags=["Modeling"])
    app.include_router(modeling_runs_router, prefix="/modeling/validation-runs", tags=["Modeling"])
    app.include_router(
        modeling_change_sets_router, prefix="/modeling/change-sets", tags=["Modeling"]
    )
    app.include_router(
        modeling_trace_links_router, prefix="/modeling/trace-links", tags=["Modeling"]
    )
    app.include_router(uml_revision_render_router, prefix="/modeling/models", tags=["Modeling"])
    app.include_router(uml_tools_router, prefix="/uml", tags=["UML"])
    app.include_router(uml_models_router, prefix="/uml", tags=["UML"])
    # Architecture containers enumeration (P4.5) — feeds fr's queue Select.
    app.include_router(
        arch_containers_router,
        prefix="/architecture/containers",
        tags=["Architecture Containers"],
    )
    # PM CRUD routers
    app.include_router(
        pm_programmes_router,
        prefix="/pm/programmes",
        tags=["PM Programmes"],
    )
    app.include_router(
        pm_projects_router,
        prefix="/pm/projects",
        tags=["PM Projects"],
    )
    app.include_router(
        pm_card_ledger_router,
        prefix="/pm/cards",
        tags=["PM Card Ledger"],
    )
    app.include_router(
        pm_resources_router,
        prefix="/pm/resources",
        tags=["PM Resources"],
    )
    app.include_router(
        pm_risks_router,
        prefix="/pm/risks",
        tags=["PM Risks"],
    )
    app.include_router(
        pm_benefits_router,
        prefix="/pm/benefits",
        tags=["PM Benefits"],
    )
    # PM catalogue: the vocabulary projects are composed from
    app.include_router(
        pm_project_phases_router, prefix="/pm/project-phases", tags=["PM Project Phases"]
    )
    app.include_router(pm_capabilities_router, prefix="/pm/capabilities", tags=["PM Capabilities"])
    app.include_router(pm_card_types_router, prefix="/pm/card-types", tags=["PM Card Types"])
    app.include_router(
        pm_project_types_router, prefix="/pm/project-types", tags=["PM Project Types"]
    )
    app.include_router(pm_catalogue_router, prefix="/pm/catalogue", tags=["PM Project Types"])
    app.include_router(pm_recipes_router, prefix="/pm/recipes", tags=["PM Recipes"])
    app.include_router(pm_wip_limits_router, prefix="/pm/wip-limits", tags=["PM WIP Limits"])
    app.include_router(
        pm_priority_levels_router, prefix="/pm/priority-levels", tags=["PM Priority Levels"]
    )
    app.include_router(
        pm_deliverable_kinds_router, prefix="/pm/deliverable-kinds", tags=["PM Deliverable Kinds"]
    )
    app.include_router(
        pm_milestone_kinds_router, prefix="/pm/milestone-kinds", tags=["PM Milestone Kinds"]
    )
    app.include_router(pm_gates_router, prefix="/pm/gates", tags=["PM Gates"])
    app.include_router(
        pm_board_templates_router, prefix="/pm/board-templates", tags=["PM Board Templates"]
    )
    # Integration tools
    app.include_router(
        integration_tools_router,
        prefix="/integration/tools",
        tags=["Integration Tools"],
    )

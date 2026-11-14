# Architecture Proposals — wire contract

> **Status (2026-10-26).** `GET /governance/architecture/proposals` was REMOVED
> with the rest of the LLM lane (no LLM dependencies remain in this service).
> The sections on the endpoint and its response are historical. Still
> authoritative: **Synthetic Artifact body** (the idempotent synthetic upsert on
> `POST /architecture/elements`) and **Realization edge body**.

> **Status (2026-05-26).** Contract defined for P4 of the
> Architecture Governance roadmap
> (`~/.claude/plans/fr-phase-5-architecture-lucky-fog.md`). Producer
> (`coding-ai`) and consumer (`fr` via `requirements_management` BFF)
> both implement against this document. No code change here — single
> source of truth before P4.1/P4.2/P4.3/P4.4 are built.

## Why this exists

P1/P2 shipped: coding-ai's `cai-architecture-classifier` now returns
ranked ArchiMate `element_type` candidates for code units, and
`requirements_management` already owns the as-designed ArchiMate model +
the synthetic-Artifact bridge convention (P2, conformance.py). What's
missing is the **proposal endpoint** the fr review queue (P4.4) calls,
which joins those two halves into "for this container, here is the
proposed binding for each detected code unit — accept to write it
upstream".

This contract is the join: it pins the shape both producers agree on so
P4.1 and P4.2 can land in parallel.

## Endpoint

```
GET /api/governance/architecture/proposals?projectId=<uuid>&container=<name>
```

Served by `requirements_management`. Joins:
- `coding-ai GET /architecture/proposals?container=<name>` (P4.1) — supplies
  `code_unit` + `ranked_candidates` per detected unit.
- WM's own `propose_realization_mappings` tool (P4.2) — supplies the
  `proposed_mapping` per unit by looking at the project's existing
  Application Components (deterministic short-circuit) and falling back
  to a new mapping agent.

**Ephemeral.** Proposals are recomputed per fetch — no DB row, no
persistence. A 5-minute in-process TTL cache keyed on
`(tenant_id, project_id, container, coding_ai_content_hash)` absorbs
double-clicks and the UI's 30s polling. Cache invalidates implicitly
when coding-ai's content hash changes (a re-analysis happens).

## Wire shape

### Request

| param      | type           | required | meaning                                                                |
|------------|----------------|----------|------------------------------------------------------------------------|
| `projectId`| `uuid` (string)| yes      | requirements_management project the proposed bindings will belong to   |
| `container`| `string`       | yes      | coding-analysis container name (e.g. `fr`, `requirements_management`)  |

Tenancy is from the license token (same as every other WM endpoint).
No body.

### Response

```jsonc
{
  "container": "fr",
  "generated_at": "2026-05-26T06:32:00Z",
  "proposals": [
    {
      "code_unit": {
        "container": "fr",                          // == request.container
        "component_id": "fr.frontend.composer",     // coding-analysis component id — primary binding key
        "name": "FrontendComposer",                 // human label
        "layer": "presentation",                    // coding-analysis layer
        "kind": "service",                          // service | module | package | interface | class | function
        "path": "frontend/src/lib/composer.ts",     // descriptive — NOT a binding key
        "node_id": "node_abc123"                    // coding-analysis graph node id (optional)
      },
      "ranked_candidates": [
        { "element_type": "application_service",   "score": 0.95 },
        { "element_type": "application_interface", "score": 0.72 }
      ],
      "proposed_mapping": {
        "kind": "bind-existing",                    // bind-existing | create-new | none
        "application_component_id": "019e628d-c088-715c-aa1a-c124026b370f",
        "draft": null
      },
      "explanation": "Composes UI panels; behavior is application-layer; matches existing Frontend Composer.",
      "already_bound": false                        // true when the deterministic short-circuit fired (saves an LLM call)
    },
    // ... one entry per detected code unit
  ]
}
```

### `proposed_mapping.kind`

- `bind-existing` — `application_component_id` is set; `draft` is `null`.
  fr's accept path POSTs ONE element (the synthetic Artifact) + ONE
  relationship (the realization edge to the existing AC).
- `create-new` — `application_component_id` is `null`; `draft` is the
  proposed AC element body (shape below). fr's accept path POSTs TWO
  elements (the new AC, then the synthetic Artifact) + ONE relationship.
- `none` — both `null`. The classifier had no usable signal; UI shows the
  candidates and the user picks/creates manually. Accept is disabled.

`draft` (when `kind = create-new`):

```jsonc
{
  "title": "Frontend Composer",                    // required
  "layer": "application",                          // ALWAYS "application" for an AC draft
  "element_type": "application_component",         // ALWAYS this
  "description": "Composes UI panels...",          // optional, LLM-generated
  "project_id": "<uuid>",                          // == request.projectId
  "properties": { /* optional, agent may add hints */ }
}
```

### `code_unit` field semantics — alignment with existing WM vocabulary

This is the **identical shape** WM's existing
`src/tools/architecture/conformance.py` already consumes for its
`code_units: list[dict]` parameter (see `analyze_conformance` and
`_artifact_binding_key` in that file). One source of truth, two callers
(conformance compute + proposal flow):

- `container` — coding-analysis container name. Required.
- `component_id` — coding-analysis component id. **Primary binding key**
  alongside `container`. `_artifact_binding_key` keys on
  `properties.code_binding = {container, component_id}` and this is the
  field the conformance tool uses to match an Artifact to a code unit.
  Any other identifier (path, file hash) is informational only.
- `name` / `layer` / `kind` / `path` — human + reverse-engineering
  metadata. The dashboard renders them; the binding does not key on them.
- `node_id` — coding-analysis graph node id when available; lets the
  overlay graph (P3.2) light up the exact node for a proposal.

### Synthetic Artifact body (what fr POSTs on accept)

```jsonc
POST /api/assets/architecture/elements
{
  "title": "<container>/<component_id>",            // e.g. "fr/fr.frontend.composer"
  "layer": "technology",
  "element_type": "artifact",
  "project_id": "<request.projectId>",
  "is_synthetic": true,                             // top-level column (already in ArchitectureElementIn)
  "properties": {
    "code_binding": {
      "container":    "<code_unit.container>",
      "component_id": "<code_unit.component_id>",   // primary binding key
      "path":         "<code_unit.path>",           // descriptive
      "kind":         "<code_unit.kind>",           // descriptive
      "node_id":      "<code_unit.node_id>"        // optional, descriptive
    }
  }
}
```

**Idempotency contract (P4.2 scope).** `POST /architecture/elements`
becomes an upsert when the body has
`is_synthetic === true` AND
`properties.code_binding.{container, component_id}` are both present:
match an existing element by `(tenant_id, project_id,
properties.code_binding.container, properties.code_binding.component_id)`;
if found, update its `title`, `description`, and `properties` (preserving
`id` + bumping `version` per the existing versioning convention) and
return 200; if not found, insert and return 201. Re-accepting the same
proposal MUST NOT create a duplicate row. The `is_synthetic` column comes
straight from the body (it's already a top-level field on
`ArchitectureElementIn`, defaulting to `false`) — no derivation from
`properties` needed.

### Realization edge body (what fr POSTs on accept, after the Artifact)

```jsonc
POST /api/assets/architecture/relationships
{
  "source_entity_type": "architecture_elements",
  "source_id":          "<artifact_id_just_created>",
  "target_entity_type": "architecture_elements",
  "target_id":          "<proposed_mapping.application_component_id>",
  "relationship_type":  "realization",
  "project_id":         "<request.projectId>"
}
```

Valid per `_validate_realization` (technology → application,
passive-structure source); verified by the P2 metamodel tests.

## Behaviour the consumer (fr P4.4) can rely on

1. The same `code_unit` is identified by `(container, component_id)` across
   refreshes, so optimistically removing a row on accept is safe even if
   polling races.
2. `ranked_candidates` is sorted by `score` descending; the queue panel
   reads `[0]` for the headline.
3. `already_bound: true` items are sent for transparency (so the user can
   see "X is already mapped to Y") but the queue panel SHOULD hide them
   by default behind a toggle — otherwise the queue is dominated by
   things the user can't act on.
4. `proposed_mapping.kind === "none"` rows render with the candidates
   visible but Accept disabled; this is the explicit "the producer
   couldn't decide for you" UX, not silent omission.
5. Errors from the upstream classifier (worker_management 5xx, timeout)
   degrade to `proposed_mapping.kind === "none"` per unit rather than
   failing the whole response. The endpoint always returns 200 when WM is
   reachable; partial-classifier-failure is conveyed in-band.

## What the contract intentionally does NOT specify

- **Reverse-engineer-baseline** (drafting Application Components from an
  empty project). Deferred per the plan; would surface as a separate
  endpoint that returns an AC list, not as proposals.
- **Multi-container batching.** v1 is one container per request; the UI
  selects one container at a time. Batching is a future optimisation.
- **Reconciliation proposals** (retiring orphan ACs, documenting
  undocumented dependencies). Out of P4 scope.

"""ArchiMate 3.2 concept / relationship / viewpoint registry.

Loaded from ``src/data/archimate/3.2/registry.json``, which
``scripts/import_archi_reference.py`` generates mechanically from a pinned Archi
checkout (``release_5.10.0``). Nothing in this module is hand-maintained
language data: legality questions are answered by looking the triple up in
Archi's own relationship matrix, never by layer order or category guesses
(findings A01/A02 of the 2026-10-26 ArchiMate plan).

Archi's model files are MIT-licensed; the notice travels inside the JSON
(``license_notice``) and is served with the registry routes.

Pure: no database, no I/O beyond reading the bundled data file once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "archimate" / "3.2" / "registry.json"

# Archi's generic relationship endpoint: where the matrix names it, ANY
# ArchiMate relationship may be the source or target (finding A10).
RELATIONSHIP_ENDPOINT = "relationship"

ACCESS_TYPES = ("write", "read", "access", "readwrite")
JUNCTION_TYPES = ("and", "or")


@dataclass(frozen=True)
class Concept:
    type: str
    archi_class: str
    layer: str
    aspect: str


@dataclass(frozen=True)
class Viewpoint:
    id: str
    name: str
    allowed_concepts: frozenset[str]
    # Empty means every relationship is allowed (Archi semantics).
    allowed_relationships: frozenset[str]

    def allows_concept(self, concept_type: str) -> bool:
        return not self.allowed_concepts or concept_type in self.allowed_concepts


@dataclass(frozen=True)
class Registry:
    language_version: str
    ruleset_id: str
    reference: dict[str, Any]
    license_notice: str
    concepts: dict[str, Concept]
    relationship_types: tuple[str, ...]
    matrix: dict[str, dict[str, frozenset[str]]]
    viewpoints: dict[str, Viewpoint]

    def allowed(self, source_type: str, target_type: str) -> frozenset[str]:
        """Relationship types Archi's matrix permits from ``source_type`` to ``target_type``.

        Either end may be :data:`RELATIONSHIP_ENDPOINT`. Unknown types get the
        empty set — an unknown concept is rejected, never waved through.
        """
        return self.matrix.get(source_type, {}).get(target_type, frozenset())

    def is_valid(self, source_type: str, relationship_type: str, target_type: str) -> bool:
        return relationship_type in self.allowed(source_type, target_type)

    def layer_of(self, concept_type: str) -> str | None:
        c = self.concepts.get(concept_type)
        return c.layer if c else None

    def types_in_layer(self, layer: str) -> frozenset[str]:
        return frozenset(c.type for c in self.concepts.values() if c.layer == layer)


# Service-defined viewpoints that predate the Archi import. They are NOT Archi
# viewpoints and are reported as ``source: "extension"``; their concept lists
# are the service's long-standing generator filters plus Archi's defaults
# (Junction, Grouping). Kept so existing views and callers stay valid.
EXTENSION_VIEWPOINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "landscape": (
        "Landscape (service extension)",
        (
            "business_service",
            "application_service",
            "technology_service",
            "business_interface",
            "application_interface",
            "technology_interface",
            "application_component",
            "node",
        ),
    ),
    "capability_map": ("Capability Map (service extension)", ("capability", "resource")),
}


@cache
def get_registry() -> Registry:
    raw = json.loads(DATA_FILE.read_text())
    concepts = {
        c["type"]: Concept(c["type"], c["archi_class"], c["layer"], c["aspect"])
        for c in raw["concepts"]
    }
    matrix = {
        src: {tgt: frozenset(rels) for tgt, rels in row.items()}
        for src, row in raw["matrix"].items()
    }
    viewpoints = {
        v["id"]: Viewpoint(
            v["id"],
            v["name"],
            frozenset(v["allowed_concepts"]),
            frozenset(v["allowed_relationships"]),
        )
        for v in raw["viewpoints"]
    }
    for vp_id, (name, types) in EXTENSION_VIEWPOINTS.items():
        viewpoints[vp_id] = Viewpoint(
            vp_id, name, frozenset(types) | {"junction", "grouping"}, frozenset()
        )
    return Registry(
        language_version=raw["language_version"],
        ruleset_id=raw["ruleset_id"],
        reference=raw["reference"],
        license_notice=raw["license_notice"],
        concepts=concepts,
        relationship_types=tuple(r["type"] for r in raw["relationship_types"]),
        matrix=matrix,
        viewpoints=viewpoints,
    )


# Module-level views for vocabulary consumers (schemas, OpenAPI enums). They
# are computed from the data file so the enums cannot drift from the matrix.
_REG = get_registry()
ARCHIMATE_ELEMENT_TYPES: frozenset[str] = frozenset(_REG.concepts)
ARCHIMATE_RELATIONSHIP_TYPES: frozenset[str] = frozenset(_REG.relationship_types)
ARCHIMATE_VIEWPOINT_IDS: frozenset[str] = frozenset(_REG.viewpoints)
ARCHI_VIEWPOINT_IDS: frozenset[str] = ARCHIMATE_VIEWPOINT_IDS - set(EXTENSION_VIEWPOINTS)
ELEMENT_TYPE_TO_LAYER: dict[str, str] = {t: c.layer for t, c in _REG.concepts.items()}

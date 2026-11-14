"""UML 2.5.1 initial profile (``uml-2.5.1-p1``) — the supported language subset.

Loaded from ``src/data/uml/2.5.1-p1/profile.json`` (target spec §8.2,
re-architecture spec §4.4). Anything outside the profile is reported by the
validator as ``UML-UNSUP-001``, so the language data here is the single answer
to "is this construct supported?" — the rules never hard-code a kind list.

Pure: no database, no I/O beyond reading the bundled data file once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "uml" / "2.5.1-p1" / "profile.json"


@dataclass(frozen=True)
class DiagramKind:
    kind: str
    object_ref: str  # "element" | "lifeline"
    connection_ref: str  # "relationship" | "message"


@dataclass(frozen=True)
class Profile:
    profile_id: str
    ruleset_id: str
    language_version: str
    reference: dict[str, Any]
    element_kinds: tuple[str, ...]
    element_features: dict[str, frozenset[str]]
    owner_kinds: dict[str, frozenset[str]]
    owner_required: frozenset[str]
    interaction_owner_kinds: frozenset[str]
    relationship_kinds: tuple[str, ...]
    aggregation_kinds: tuple[str, ...]
    connector_kinds: tuple[str, ...]
    visibilities: tuple[str, ...]
    parameter_directions: tuple[str, ...]
    # specific kind -> kinds it may specialize (UML Classifier::maySpecializeType).
    generalization_compatible: dict[str, frozenset[str]]
    lifeline_type_kinds: frozenset[str]
    message_kinds: tuple[str, ...]
    synchronous_message_kinds: frozenset[str]
    fragment_operators: tuple[str, ...]
    guarded_operators: frozenset[str]
    single_operand_operators: frozenset[str]
    diagram_kinds: dict[str, DiagramKind]

    def may_specialize(self, specific: str, general: str) -> bool:
        return general in self.generalization_compatible.get(specific, frozenset())


@cache
def get_profile() -> Profile:
    raw = json.loads(DATA_FILE.read_text())
    return Profile(
        profile_id=raw["profile_id"],
        ruleset_id=raw["ruleset_id"],
        language_version=raw["language_version"],
        reference=raw["reference"],
        element_kinds=tuple(raw["element_kinds"]),
        element_features={k: frozenset(v) for k, v in raw["element_features"].items()},
        owner_kinds={k: frozenset(v) for k, v in raw["owner_kinds"].items()},
        owner_required=frozenset(raw["owner_required"]),
        interaction_owner_kinds=frozenset(raw["interaction_owner_kinds"]),
        relationship_kinds=tuple(raw["relationship_kinds"]),
        aggregation_kinds=tuple(raw["aggregation_kinds"]),
        connector_kinds=tuple(raw["connector_kinds"]),
        visibilities=tuple(raw["visibilities"]),
        parameter_directions=tuple(raw["parameter_directions"]),
        generalization_compatible={
            k: frozenset(v) for k, v in raw["generalization_compatible"].items()
        },
        lifeline_type_kinds=frozenset(raw["lifeline_type_kinds"]),
        message_kinds=tuple(raw["message_kinds"]),
        synchronous_message_kinds=frozenset(raw["synchronous_message_kinds"]),
        fragment_operators=tuple(raw["fragment_operators"]),
        guarded_operators=frozenset(raw["guarded_operators"]),
        single_operand_operators=frozenset(raw["single_operand_operators"]),
        diagram_kinds={
            k: DiagramKind(k, v["object"], v["connection"]) for k, v in raw["diagram_kinds"].items()
        },
    )

"""Regenerate ``src/data/archimate/3.2/registry.json`` from a pinned Archi checkout.

The registry is the ONLY place the service learns which ArchiMate concepts,
relationship types, relationship triples and viewpoints exist. It is data,
derived mechanically from Archi's own model files, so a reviewer can diff a
version bump instead of re-reading hand-written rules.

Usage (from the repo root):

    git clone --branch release_5.10.0 https://github.com/archimatetool/archi.git /path/archi
    uv run python scripts/import_archi_reference.py --archi-root /path/archi

The script refuses to run against a checkout whose HEAD is not the pinned
commit unless ``--allow-other-commit`` is passed — an upgrade is a deliberate,
reviewed act (plan §12), never a side effect of whatever was cloned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PINNED_TAG = "release_5.10.0"
PINNED_COMMIT = "f757b06d5f75b5565786bb0679c75450cc3a577f"
ARCHI_VERSION = "5.10.0"
LANGUAGE_VERSION = "3.2"

MODEL = "com.archimatetool.model"
FILES = {
    "relationships.xml": f"{MODEL}/model/relationships.xml",
    "relationships-keys.xml": f"{MODEL}/model/relationships-keys.xml",
    "viewpoints.xml": f"{MODEL}/model/viewpoints.xml",
    "ArchimateModelUtils.java": f"{MODEL}/src/com/archimatetool/model/util/ArchimateModelUtils.java",
    "LICENSE.txt": f"{MODEL}/LICENSE.txt",
}

# Archi's grouping methods (ArchimateModelUtils.get*Classes) → service layer.
# "physical" stays a separate classification, as the service always had it;
# Location and Grouping (getOtherClasses) and Junction (getConnectorClasses)
# go to "other", a classification the legacy layer enum lacked (finding A10).
GROUP_TO_LAYER = {
    "getStrategyClasses": "strategy",
    "getBusinessClasses": "business",
    "getApplicationClasses": "application",
    "getTechnologyClasses": "technology",
    "getPhysicalClasses": "physical",
    "getMotivationClasses": "motivation",
    "getImplementationMigrationClasses": "implementation_migration",
    "getOtherClasses": "other",
    "getConnectorClasses": "other",
}

# Archi carries no aspect data. This is the service's own long-standing
# classification (tools/architecture/metamodel.py before 2026-10-26), extended
# to the four concepts it lacked. It is descriptive only: legality comes from
# the matrix, never from the aspect.
ASPECT = {
    **dict.fromkeys(
        [
            "business_actor",
            "business_role",
            "business_collaboration",
            "business_interface",
            "application_component",
            "application_collaboration",
            "application_interface",
            "node",
            "device",
            "system_software",
            "technology_collaboration",
            "technology_interface",
            "path",
            "communication_network",
            "equipment",
            "facility",
            "distribution_network",
            "resource",
        ],
        "active_structure",
    ),
    **dict.fromkeys(
        [
            "business_process",
            "business_function",
            "business_interaction",
            "business_event",
            "business_service",
            "application_function",
            "application_interaction",
            "application_process",
            "application_event",
            "application_service",
            "technology_function",
            "technology_process",
            "technology_interaction",
            "technology_event",
            "technology_service",
            "capability",
            "value_stream",
            "course_of_action",
            "work_package",
            "implementation_event",
        ],
        "behavior",
    ),
    **dict.fromkeys(
        [
            "business_object",
            "contract",
            "representation",
            "data_object",
            "artifact",
            "material",
            "deliverable",
            "gap",
        ],
        "passive_structure",
    ),
    **dict.fromkeys(
        [
            "stakeholder",
            "driver",
            "assessment",
            "goal",
            "outcome",
            "principle",
            "requirement",
            "constraint",
            "meaning",
            "value",
        ],
        "motivation",
    ),
    **dict.fromkeys(["product", "plateau", "grouping", "location"], "composite"),
    "junction": "connector",
}

MACROS = {
    "$StrategyElements$": "strategy",
    "$BusinessElements$": "business",
    "$ApplicationElements$": "application",
    "$TechnologyElements$": "technology",
    "$PhysicalElements$": "physical",
    "$MotivationElements$": "motivation",
    "$ImplementationMigrationElements$": "implementation_migration",
}

# Archi's Viewpoint.defaultList: always allowed in every viewpoint.
VIEWPOINT_DEFAULT_CONCEPTS = ("junction", "grouping")


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_groups(utils_java: str) -> dict[str, list[str]]:
    """Map each Archi grouping method to its ordered EClass names."""
    groups: dict[str, list[str]] = {}
    for method in GROUP_TO_LAYER:
        m = re.search(rf"public static EClass\[\] {method}\(\)\s*\{{(.*?)\}};", utils_java, re.S)
        if m is None:
            raise SystemExit(f"cannot find {method} in ArchimateModelUtils.java")
        groups[method] = re.findall(r"eINSTANCE\.get([A-Z][A-Za-z]*)\(\)", m.group(1))
    return groups


def build(archi_root: Path) -> dict:
    paths = {k: archi_root / v for k, v in FILES.items()}
    for k, p in paths.items():
        if not p.is_file():
            raise SystemExit(f"missing {k}: {p}")

    groups = parse_groups(paths["ArchimateModelUtils.java"].read_text())
    concepts = []
    for method, classes in groups.items():
        for cls in classes:
            t = snake(cls)
            concepts.append(
                {
                    "type": t,
                    "archi_class": cls,
                    "layer": GROUP_TO_LAYER[method],
                    "aspect": ASPECT[t],
                }
            )
    by_class = {c["archi_class"]: c["type"] for c in concepts}

    keys_root = ET.parse(paths["relationships-keys.xml"]).getroot()
    key_to_rel: dict[str, str] = {}
    relationship_types = []
    for k in keys_root.findall("key"):
        cls = k.attrib["relationship"]
        rel = snake(cls.removesuffix("Relationship"))
        key_to_rel[k.attrib["char"]] = rel
        relationship_types.append({"type": rel, "archi_class": cls, "key": k.attrib["char"]})

    rel_root = ET.parse(paths["relationships.xml"]).getroot()
    if rel_root.attrib.get("version") != LANGUAGE_VERSION:
        raise SystemExit(f"relationships.xml version {rel_root.attrib.get('version')!r}")

    def endpoint(cls: str) -> str:
        # Archi's generic "Relationship" endpoint: any relationship may stand here.
        if cls == "Relationship":
            return "relationship"
        if cls not in by_class:
            raise SystemExit(f"matrix names unknown concept {cls}")
        return by_class[cls]

    matrix: dict[str, dict[str, list[str]]] = {}
    for src in rel_root.findall("source"):
        row = matrix.setdefault(endpoint(src.attrib["concept"]), {})
        for tgt in src.findall("target"):
            letters = tgt.attrib.get("relations", "")
            # Upper case would mark a "direct" (non-derived) relationship in
            # Archi's encoding. 5.10.0 ships none, so the direct/derived split
            # cannot be taken from this file (plan §5.1) and is not recorded.
            if letters != letters.lower():
                raise SystemExit("matrix now carries direct/derived case; review before import")
            row[endpoint(tgt.attrib["concept"])] = sorted(key_to_rel[c] for c in letters)

    vp_root = ET.parse(paths["viewpoints.xml"]).getroot()
    layer_members: dict[str, list[str]] = {}
    for c in concepts:
        layer_members.setdefault(c["layer"], []).append(c["type"])
    viewpoints = []
    for vp in vp_root.findall("viewpoint"):
        allowed: set[str] = set(VIEWPOINT_DEFAULT_CONCEPTS)
        for el in vp.findall("concept"):
            token = (el.text or "").strip()
            if token in MACROS:
                allowed.update(layer_members[MACROS[token]])
            elif token.endswith("Relationship"):
                raise SystemExit("viewpoints.xml now restricts relationships; review before import")
            else:
                allowed.add(endpoint(token))
        name = vp.find("name")
        viewpoints.append(
            {
                "id": vp.attrib["id"],
                "name": (name.text or "").strip() if name is not None else vp.attrib["id"],
                "allowed_concepts": sorted(allowed),
                # Archi: an empty relationship list allows every relationship.
                "allowed_relationships": [],
            }
        )

    return {
        "language": "archimate",
        "language_version": LANGUAGE_VERSION,
        "ruleset_id": f"archimate-{LANGUAGE_VERSION}/archi-{ARCHI_VERSION}",
        "reference": {
            "tool": "Archi",
            "version": ARCHI_VERSION,
            "tag": PINNED_TAG,
            "commit": PINNED_COMMIT,
            "files": {k: sha256(p) for k, p in sorted(paths.items())},
        },
        "license_notice": paths["LICENSE.txt"].read_text().strip(),
        "concepts": concepts,
        "relationship_types": relationship_types,
        "matrix": {s: dict(sorted(t.items())) for s, t in sorted(matrix.items())},
        "viewpoints": viewpoints,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archi-root", type=Path, required=True)
    ap.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src/data/archimate/3.2/registry.json",
    )
    ap.add_argument("--allow-other-commit", action="store_true")
    args = ap.parse_args()

    head = subprocess.run(
        ["git", "-C", str(args.archi_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != PINNED_COMMIT and not args.allow_other_commit:
        print(f"archi HEAD {head} != pinned {PINNED_COMMIT}", file=sys.stderr)
        return 2

    data = build(args.archi_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n")
    print(
        f"wrote {args.output}: {len(data['concepts'])} concepts, "
        f"{len(data['relationship_types'])} relationship types, "
        f"{sum(len(t) for t in data['matrix'].values())} matrix cells, "
        f"{len(data['viewpoints'])} viewpoints"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

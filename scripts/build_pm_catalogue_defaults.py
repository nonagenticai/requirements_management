"""Build ``src/data/pm_catalogue_defaults.json`` — the default PM catalogue.

Sources (outside this repo, so the JSON is committed and this script is run by hand when a
source changes; CI never runs it):

* ``scripts/sources/trigger_registry.md`` — a vendored copy of
  ``n8n_custom_nodes/AA_IMPLEMENTATION/trigger_registry.md`` @ d469ca1c6 (see its header)
  PLUS the 4 py2rust ``rewrite_*`` card types added 2026-10-26 (RS-D4, George 10-25): project
  types (George's quoted ``> name - …`` lines), capabilities
  (the registry's "<X> Card Type Catalogue" sections) and every card type with its
  description, in its section's capability. capabilities: trigger_registry.md is the only
  source (George 2026-10-13) — the capability set and every card type's capability come
  from it and nothing else. R183: the registry wins over the RM catalogue.
* ``n8n-designs/design/choices/v4/capabilities/*.yaml`` — EVERY capability file (R228) — the
  conveyor v4 card-type designs (R193): each placed card type's ``inputs``, ``outputs``,
  ``acceptance`` (mapping below, ``V4 → RM``) and ``phase``. Archetype siblings inherit
  ``outputs`` per (list, slot) from their reference type (the design's YAML contract, as
  conveyor ``wfgen.load_design``). Pass the dir with ``--v4`` / ``$PM_CATALOGUE_V4_DIR``.
* ``scripts/sources/landing_predicates.json`` — a vendored copy of
  ``n8n_custom_nodes/AA_IMPLEMENTATION/landing_predicates.json`` @ e21ee10f0: each card type's
  definition of done; a card type it does not name gets NULL ("not specified").
* Tier 2 (WIP limits, priority levels, deliverable / milestone kinds, gates, board
  templates) — constants below, each citing its source.
* George 10-13 v4 rulings — constants below, kept apart from the registry-vendored data:
  ruling 2 (D16) the ``setup`` system phase; ruling 9 the ``kanban_coach`` capability and
  the ``project_phase_start`` / ``project_phase_closure`` card types; R184 the ONE IT
  project type ``it`` (the registry's own row); R185 its
  first-build phases, since George 2026-11-03 ``setup`` → ``preliminary`` → ``analysis`` →
  ``design`` → ``execution`` → ``test`` (closure is fr's, not RM's) and matrix; R88 its entry
  card type; R188 / R190 the board template fields; ruling 12 / D4 / ruling 10 the board
  template is RM's own master. ⛔ Scope: program / meta_prompt / meta_procedure / kaizen
  stay exactly as before v4 (no phases, no card types).
* George 2026-10-31 Kata K15: the ``kata`` project type (``KATA_TYPE``): ``it``'s first-build phases
  (setup, preliminary, execution),
  ``it``'s setup placement and entry, and (Kata WP4, George 2026-11-01) the four kata-only
  preliminary ``workflow_engineering`` card types plus (Kata WP5, George 2026-11-03) the one
  execution-phase type ``wf_change`` (``KATA_CARD_TYPES``).
* George 2026-11-03: the four ``rewrite_*`` card types of a language rewrite (RS-D4). Three of
  them are not in the v4 card_types, so their definitions are held here, copied from the live
  conveyor-tenant rows (``REWRITE_CARD_TYPE_DEFINITIONS``). George 2026-11-07: the rewrite chain
  is an ``it`` flow and there is NO ``py2rust`` project type — the three held types sit in
  ``it``'s ``execution`` phase (``IT_REWRITE_IN``), where the live conveyor tenant places them.

V4 → RM card type mapping (R193). A v4 capability YAML defines, per card type, the ruled
output schema of each of the 16 agent slots (``outputs.<ct>.<LIST>.<slot>``, LIST in
READY/DOING/QA/DONE, slot in readying/doing/inspecting/releasing). RM's ``card_types`` has
three list-of-string columns. They are filled mechanically, in YAML order, deduplicated:

* ``inputs`` = the card type's entry in the YAML's explicit top-level ``inputs:`` section
  (R194), in any of the capability files; a card type not listed there has ``[]``. The names
  are RM ProjectBrief fields (what the brief must answer). ⛔ R194: READY.readying output
  keys are NOT inputs — they are context the step gathers (ids, versions), not brief data.
* ``outputs`` = the keys of every list's ``releasing`` output except ``handoff_comment_id``
  (every releasing step writes one; it is the hand-off, not a product) — the ids of what
  the card saved: artifacts, brief versions, cards, the phase it set, the PR, the merge.
* ``acceptance`` = every list's ``inspecting`` ``checks``, as ``"<LIST>: <check>"`` — the
  named checks R129 requires to pass (any ``pass: false`` blocks).

``rules`` are the ``CARD_TYPE_RULES`` below only (v3's ``rules`` are gone);
``description`` stays the registry's text (R183; the YAML ``text`` must equal it).
A project type's required inputs (R193) = the union of its entry card types' ``inputs``;
RM's schema has no column for it (``GET /pm/project-types/{eid}`` returns phases, card type
ids and entry card type ids), so it is derived, never stored.

Card types: only those a default project type places are emitted. R228 (George 2026-10-14):
``it`` places every card type the v4 design defines, each in its design ``phase`` (see
``it_matrix``), so a registry card type is a default iff the design defines it (the four
non-IT types place none; ``sources.unplaced_card_types`` counts the rest). ⛔ The KATA-ONLY
``workflow_engineering`` types are the exception to R228: ``kata`` places them, ``it`` never
does (``KATA_CARD_TYPES``). Capabilities stay the full registry set plus ``kanban_coach``.

Usage::

    uv run python scripts/build_pm_catalogue_defaults.py \
        [--registry PATH] [--v4 DIR] [--predicates PATH] [--out PATH] [--check] [--allow-shrink]

``--check`` exits 1 if the committed JSON differs from what the sources produce.
Shrink guard: it refuses to write (and ``--check`` reports) fewer card types than ``--out`` holds, unless ``--allow-shrink``.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

MOVIES = Path(__file__).resolve().parents[1]
while MOVIES.name != "Movies" and MOVIES != MOVIES.parent:
    MOVIES = MOVIES.parent
# Vendored (scripts/sources/README.md names each source commit) so the build never reads a
# stale sibling checkout.
DEFAULT_REGISTRY = Path(__file__).resolve().parent / "sources/trigger_registry.md"
# The v4 design dir: ``--v4``, else ``$PM_CATALOGUE_V4_DIR``, else the sibling n8n-designs
# checkout — the design's only home since 2026-10-31 (R335; ``n8n-local`` is gone).
DEFAULT_V4 = Path(os.environ.get("PM_CATALOGUE_V4_DIR") or MOVIES / "n8n-designs/design/choices/v4")
DEFAULT_PREDICATES = Path(__file__).resolve().parent / "sources/landing_predicates.json"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src/data/pm_catalogue_defaults.json"

# Registry section heading → capability name. Headings carry typos in the source
# ("Imrprovement"), and two sections name the improvement project they serve rather than
# a capability, so the mapping is explicit rather than derived.
SECTIONS: dict[str, tuple[str, str, str]] = {
    "Program Management Card Type Catalogue": (
        "program_management",
        "Program management",
        "Card types that deal with program management.",
    ),
    "Project Management Type Catalogue": (
        "project_management",
        "Project management",
        "Card types that deal with project management.",
    ),
    "Change Management Card Type Catalogue": (
        "change_management",
        "Change management",
        "Card types that deal with change management.",
    ),
    "Prompt Improvement Card Type Catalogue": (
        "meta_prompt",
        "Prompt improvement",
        "Card types for prompt improvement in flows.",
    ),
    "Procedure Imrprovement Card Type Catalogue": (
        "meta_procedure",
        "Procedure improvement",
        "Card types for procedure improvements for agents.",
    ),
    "Kaizen Card Type Catalogue": ("kaizen", "Kaizen", "Card types for flow improvements."),
    "Research Card Type Catalogue": ("research", "Research", "Card types for research."),
    "Analysis Card Type Catalogue": ("analysis", "Analysis", "Card types for analysis."),
    "Architecture Card Type Catalogue": (
        "architecture",
        "Architecture",
        "Card types for architecture.",
    ),
    "Design Card Type Catalogue": ("design", "Design", "Card types for design."),
    "Testing Card Type Catalogue": ("testing", "Testing", "Card types for testing."),
    "BDDTDD Card Type Catalogue": (
        "bdd_tdd",
        "BDD / TDD",
        "Card types for behaviour- and test-driven development.",
    ),
    "Development Card Type Catalogue": (
        "development",
        "Development",
        "Card types for development.",
    ),
    "Refactoring Card Type Catalogue": (
        "refactoring",
        "Refactoring",
        "Card types for refactoring.",
    ),
    # The registry's category list does not name this section yet ("waits for the vocabulary
    # ruling"); the name is the heading's, snake-cased.
    "Asset Library Statistics Card Type Catalogue": (
        "asset_library_statistics",
        "Asset library statistics",
        "Card types for principle, requirement and decision library statistics.",
    ),
    # Kata WP4 (George 2026-11-01, K3): the kata's own work. The registry's category list names it
    # ("workflow_engineering - card types that deal with changing the workflow itself").
    "Workflow Engineering Card Type Catalogue": (
        "workflow_engineering",
        "Workflow engineering",
        "Card types for changing the workflow itself: observe, probe, root cause and andon scope.",
    ),
}

# Typos in registry card-type names, fixed in the name only (the description is kept).
NAME_FIXES = {
    "prompt_performanc_statistics_analysis": "prompt_performance_statistics_analysis",
}

ENTRY = re.compile(r"^([a-z][a-z0-9_]*)\s+-\s+(.*)$")
# A section heading may carry a bracketed note: "Asset Library Statistics Card Type Catalogue
# (added 2026-09-30 from the roster; …)".
HEADING_NOTE = re.compile(r"\s*\(.*\)\s*$")
# A card type row's provenance note: "… (added 2026-09-30 from the roster)".
ADDED_NOTE = re.compile(r"\s*\(added [^()]*\)\s*$")


def _heading(line: str) -> str:
    return HEADING_NOTE.sub("", line.strip())


def _title(name: str) -> str:
    return name.replace("_", " ").capitalize()


def parse_registry(text: str) -> tuple[list[dict], list[dict], list[dict]]:
    lines = [ln.rstrip() for ln in text.splitlines()]
    project_types: list[dict] = []
    i = lines.index("Project type catalogue") + 1
    while i < len(lines) and lines[i].strip():
        # George's lines are quoted ("> program - …").
        m = ENTRY.match(lines[i].strip().removeprefix(">").strip())
        if m:
            project_types.append({"name": m[1], "title": _title(m[1]), "description": m[2].strip()})
        i += 1

    capabilities: list[dict] = []
    card_types: list[dict] = []
    current: str | None = None
    for raw in lines:
        line = raw.strip()
        if _heading(line) in SECTIONS:
            name, title, desc = SECTIONS[_heading(line)]
            capabilities.append({"name": name, "title": title, "description": desc})
            current = name
            continue
        if line.startswith("Board list trigger register"):
            break
        m = ENTRY.match(line)
        if current and m:
            ct = NAME_FIXES.get(m[1], m[1])
            card_types.append(
                {
                    "name": ct,
                    "title": _title(ct),
                    "description": m[2].strip(),
                    "capability": current,
                }
            )
    missing = set(SECTIONS) - {_heading(ln) for ln in lines}
    if missing:
        raise SystemExit(f"registry sections not found: {sorted(missing)}")
    names = [c["name"] for c in card_types]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"duplicate card types in registry: {sorted(dupes)}")
    return project_types, capabilities, card_types


# Card types that are a human's task. v4 has none: every sign-off is an fr approval asked
# by an agent card (ask_fr), so the card itself is not a human task.
HUMAN_TASKS: set[str] = set()

# ---------------------------------------------------------------------------
# George 10-13 v4 rulings: a second, explicit source, kept apart from the registry data
# ---------------------------------------------------------------------------

# v4 ruling 2 (D16 / R41): the system phase every project starts in.
SETUP_PHASE: dict = {
    "name": "setup",
    "title": "Setup",
    "description": (
        "The system phase every project starts in, before its first work phase "
        "(George 10-13 v4 ruling 2)."
    ),
    "output": None,
    "exit_rule": None,
}
# R185: the first-build work phases of ``it``; George 2026-11-03: setup, preliminary, analysis,
# design, execution, test (closure stays out of RM; fr adds it). The output is the ruled product
# of the phase's work cards; the exit rule is R88 + R92 (the last released card starts the
# closure; the closure, after an approved phase sign-off, starts the next phase or, in the last
# phase, project_closure). setup keeps NULL (R90: project_setup sets the phase).
_PHASE_EXIT = (
    "project_phase_closure released after an approved phase sign-off; it starts the next "
    "phase (R88, R92)"
)
V4_PHASES: list[dict] = [
    SETUP_PHASE,
    {
        "name": "preliminary",
        "title": "Preliminary",
        "description": (
            "Research and change management before the analysis: frame what the later phases "
            "act on (R185; George 2026-11-03)."
        ),
        "output": "published research and change-management documents (research, change_management)",
        "exit_rule": _PHASE_EXIT,
    },
    {
        "name": "analysis",
        "title": "Analysis",
        "description": "Find what the work acts on (George 2026-11-03).",
        "output": "a published findings report, every finding with its status (bug_analysis, R163)",
        "exit_rule": (
            "project_phase_closure released after an approved phase sign-off; approval "
            "approves every confirmed finding (R88, R92, R186)"
        ),
    },
    {
        "name": "design",
        "title": "Design",
        "description": (
            "Shape the work before it is built: architecture, design and the tests that come "
            "first (George 2026-11-03)."
        ),
        "output": (
            "published architecture and design documents and RM assets, and the tests written "
            "first (architecture, design, bdd_tdd)"
        ),
        "exit_rule": _PHASE_EXIT,
    },
    {
        "name": "execution",
        "title": "Execution",
        "description": "The work: act on what the earlier phases found (R185).",
        "output": "one merged pull request per confirmed finding (development_bug_fix, R167)",
        "exit_rule": _PHASE_EXIT,
    },
    {
        "name": "test",
        "title": "Test",
        "description": "Test what the execution phase built (George 2026-11-03).",
        "output": "published testing documents and test reports, and merged tests (testing)",
        "exit_rule": (
            "project_phase_closure released after an approved phase sign-off; it creates "
            "project_closure (R88, R92)"
        ),
    },
]

# v4 ruling 9: exactly these three additions and nothing more. The kanban_coach text is
# verbatim from its ``purpose`` as first designed (n8n-local afcf981); that yaml was removed
# in 22c307e and the capability is now a row of n8n-designs design/choices/v4/model.md. The two
# card types are verbatim from design/choices/v4/capabilities/project_management.yaml
# (``card_types.<name>.text``). Neither source gives a title, so the title is the name's, as
# for every registry row.
V4_CAPABILITIES: list[dict] = [
    {
        "name": "kanban_coach",
        "title": "Kanban coach",
        "description": (
            "Protector of flow. Any card interruption invokes a card for the kanban coach. "
            "Owns all flow-related decisions and fr/human interaction during a phase: blocked "
            "cards, comments, human input and answers, in-flow approvals, and rework limit "
            "reached (R34)."
        ),
    },
]
V4_CARD_TYPES: list[dict] = [
    {
        "name": "project_phase_start",
        "title": "Project phase start",
        "description": (
            "each phase decisions of which capabilities and card types will be needed and "
            "creating the initial set of cards for that phase"
        ),
        "capability": "project_management",
    },
    {
        "name": "project_phase_closure",
        "title": "Project phase closure",
        "description": (
            "closing out the phase which also triggers the start of the next phase (or better "
            "asks the user to approve the phase)"
        ),
        "capability": "project_management",
    },
    # phase_coverage_check: retired by R347 (4).
]

# R184: the ONE IT project type, the registry's own ``it`` row. R185: its phases. R88: the
# entry card type, created by the computed start step (R39).
#
# R228 (George 2026-10-14): ``it`` allows EVERY card type the v4 design defines (63 at the
# design of 2026-10-14). The matrix is therefore built, not listed:
#
# * ``IT_PM_MATRIX`` — the project-management placements, fixed (R185): setup =
#   project_start_request_analysis + project_setup; every work phase gets
#   project_phase_start + project_phase_closure (phase_coverage_check: retired by R347 (4));
#   project_closure sits in the last phase
#   (test since George 2026-11-03, R88). These types carry no ``phase:`` in the design.
# * ``IT_PHASE_OF_TYPE`` — named exceptions, applied first: choice_option_development stays in
#   execution (R331); decision_development goes with it, because its ``upstream`` is
#   choice_option_development and an upstream card in a LATER phase can never be DONE;
#   tdd_test_development stays in execution, paired with its development card (R250, P-W5-5).
# * ``IT_ALSO_IN`` — a second placement of a type already placed: development_bug_fix also in
#   test, so testing_execution findings can still become fix cards in the last phase.
# * ``IT_PHASE_OF_CAPABILITY`` — George 2026-11-03 "move by capability": analysis → analysis;
#   architecture, design, bdd_tdd → design; testing → test.
# * ``IT_PHASE_OF_UNPHASED`` — the wave-1 reference type whose design entry has no ``phase:``
#   key keeps its R185 placement: development_bug_fix (execution). (bug_analysis, the other
#   one, is placed by its capability.)
# * every other v4-defined card type (research, change_management, development, refactoring)
#   goes to the phase its capability YAML entry states (``card_types.<name>.phase``). A defined
#   type with no phase, or a phase ``it`` does not have, is a SystemExit — never silently
#   dropped.
IT_TYPE = "it"
IT_PHASES: tuple[str, ...] = ("setup", "preliminary", "analysis", "design", "execution", "test")
_PM_PHASE = ["project_phase_start", "project_phase_closure"]
IT_PM_MATRIX: dict[str, list[str]] = {
    "setup": ["project_start_request_analysis", "project_setup"],
    **{p: list(_PM_PHASE) for p in IT_PHASES[1:-1]},
    IT_PHASES[-1]: [*_PM_PHASE, "project_closure"],
}
IT_PHASE_OF_TYPE: dict[str, str] = {
    "choice_option_development": "execution",
    "decision_development": "execution",
    "tdd_test_development": "execution",
}
IT_ALSO_IN: dict[str, list[str]] = {"test": ["development_bug_fix"]}
IT_PHASE_OF_CAPABILITY: dict[str, str] = {
    "analysis": "analysis",
    "architecture": "design",
    "design": "design",
    "bdd_tdd": "design",
    "testing": "test",
}
IT_PHASE_OF_UNPHASED: dict[str, str] = {
    "development_bug_fix": "execution",
}
IT_ENTRY: dict[str, list[str]] = {"setup": ["project_start_request_analysis"]}
# George 2026-11-07: the rewrite chain is an `it` flow (no py2rust project type). Its work cards
# sit in execution — the placement the live conveyor tenant has.
IT_REWRITE_IN: dict[str, list[str]] = {
    "execution": ["rewrite_cutover_readiness", "rewrite_parity_qa", "rewrite_slice"],
}

# Kata (George 2026-10-31, K1-K21; n8n-designs design/choices/v4/kata_v1.html + kata/wp3-entry.html +
# kata/wp4-grasp.html): the kanban coach opens a project of type ``kata`` when a block is a conveyor
# defect. K15: it keeps ``setup`` so PM's capability selection runs unchanged, and has ``it``'s
# first-build phases (setup, preliminary, execution) — NOT the 2026-11-03 analysis / design /
# test: project_phase_start's Plan gate sends a phase with zero cards to BLOCKED, so an empty
# phase would strand every kata project. WP3 places what setup needs (``it``'s setup placement and entry); WP4 (George 2026-11-01)
# adds the four preliminary ``workflow_engineering`` card types (K3). Not a registry row: a v4
# addition like ruling 9's, kept apart from the vendored data.
KATA_TYPE: dict[str, str] = {
    "name": "kata",
    "title": "Kata",
    "description": (
        "A short project the kanban coach opens when a card is blocked by a conveyor defect: "
        "find the root cause, change the workflow, prove it by replaying the failing input, "
        "publish it, and let the stopped project resume (Kata K1-K21)."
    ),
}
# ⛔ KATA-ONLY (George 2026-11-01, Kata WP4): these four card types exist ONLY on ``kata``. R228
# places every v4-defined card type on ``it``; this is the EXPLICIT exception (``it_matrix``
# skips them), so an ordinary ``it`` project's matrix stays exactly what it was.
# Kata WP5 (George 2026-11-03, n8n-designs kata/wp5-change-replay.html) adds ``wf_change``, the ONE
# execution-phase kata card type.
KATA_PRELIMINARY: frozenset[str] = frozenset(
    {"wf_observe", "wf_probe", "wf_root_cause", "wf_andon_scope"}
)
KATA_EXECUTION: frozenset[str] = frozenset({"wf_change"})
KATA_CARD_TYPES: frozenset[str] = KATA_PRELIMINARY | KATA_EXECUTION
KATA_MATRIX: dict[str, list[str]] = {
    "setup": list(IT_PM_MATRIX["setup"]),
    "preliminary": sorted(KATA_PRELIMINARY),
    "execution": sorted(KATA_EXECUTION),
}

# The rewrite chain's work card types (RS-D4). The v4 capability YAML defines no card_types /
# outputs entry for these three (only QA criteria under capability_flows), so their definitions
# are held here, copied verbatim from the LIVE conveyor-tenant rows (RM requirements_api, tenant
# 5f868d42-03f8-51f0-a93c-3e83070dc196, read 2026-11-03), rules and definition_of_done
# included: neither CARD_TYPE_RULES' source nor the vendored landing predicates name them. Once
# the design YAML defines one, the build refuses it. George 2026-11-07: placed on ``it``'s
# execution (``IT_REWRITE_IN``); there is no py2rust project type.
REWRITE_CARD_TYPE_DEFINITIONS: dict[str, dict] = {
    "rewrite_slice": {
        "capability": "development",
        "text": None,
        "phase": None,
        "inputs": [],
        "outputs": [
            "commit_sha",
            "pr_number",
            "pr_url",
            "head_sha",
            "fix_report_artifact_id",
            "merge_sha",
        ],
        "acceptance": [
            "READY: planned_files_allowed",
            "DOING: diff_only_planned_files",
            "DOING: workspace_test_run_green",
            "DOING: openapi_diff_slice_routes_present_and_equal",
            "QA: change_meets_acceptance",
        ],
        "rules": ["repo_write"],
        "definition_of_done": "pr_merged",
    },
    "rewrite_parity_qa": {
        "capability": "testing",
        "text": None,
        "phase": None,
        "inputs": [],
        "outputs": ["draft_artifact_id", "report_artifact_id", "published_artifact_id"],
        "acceptance": [
            "READY: source_shas_pinned",
            "DOING: schema_diff_equal",
            "DOING: openapi_diff_equal_to_source_union",
            "DOING: behaviour_replay_equal",
            "QA: parity_computed_without_judgement",
            "DONE: artifact_is_qa_version",
        ],
        "rules": ["container_read_exec"],
        "definition_of_done": "asset_persisted",
    },
    "rewrite_cutover_readiness": {
        "capability": "change_management",
        "text": None,
        "phase": None,
        "inputs": [],
        "outputs": ["draft_artifact_id", "report_artifact_id", "published_artifact_id"],
        "acceptance": [
            "READY: parity_qa_released",
            "DOING: parity_results_true_on_pinned_shas",
            "DOING: data_copy_plan_present",
            "QA: g4_evidence_complete",
            "DONE: artifact_is_qa_version",
        ],
        "rules": [],
        "definition_of_done": "asset_persisted",
    },
}

# R228: every capability file in the design directory is read (not a fixed list). These
# three must be present, or the directory is not a v4 design at all (vacuity guard).
V4_REQUIRED_FILES = ("project_management", "analysis", "development")
# The design's archetype contract, mirrored from conveyor ``conveyor/wfgen.py``
# (ARCHETYPE_OF_CAPABILITY / ARCHETYPE_BASE, proposals/wave3-card-types.md "YAML contract"):
# a type's archetype is its ``archetype:`` key, else its file's default; a type with no
# ``outputs`` entry (or a partial one) inherits every (list, slot) it does not name from its
# archetype's reference type.
ARCHETYPE_OF_CAPABILITY = {
    "analysis": "analysis",
    "development": "development",
    "refactoring": "development",
    "research": "document",
    "architecture": "document",
    "design": "document",
}
ARCHETYPE_BASE = {
    "analysis": "bug_analysis",
    "development": "development_bug_fix",
    "document": "research_spike",
    "rm_asset": "principle_development",
}
# Design capability name → registry capability name, spelling only. The registry section
# "BDDTDD Card Type Catalogue" is snake-cased ``bdd_tdd`` here (SECTIONS); the design file is
# ``bddtdd.yaml`` with ``capability: bddtdd``. Same capability, so it is aliased, not re-mapped.
CAPABILITY_ALIASES = {"bddtdd": "bdd_tdd"}
LISTS = ("READY", "DOING", "QA", "DONE")
# Written by every releasing step: the hand-off comment, not a product of the card.
OUTPUT_EXCLUDE = frozenset({"handoff_comment_id"})


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _inherit(own: dict | None, base: dict | None) -> dict | None:
    """{list: {slot: x}}: own's slots win, every other list/slot is base's (wfgen._inherit)."""
    if base is None:
        return own
    if own is None:
        return base
    out: dict = {}
    for lst in [*base, *[x for x in own if x not in base]]:
        b, o = base.get(lst) or {}, own.get(lst) or {}
        out[lst] = (
            {**b, **o}
            if isinstance(b, dict) and isinstance(o, dict)
            else own.get(lst, base.get(lst))
        )
    return out


def parse_v4(v4: Path) -> dict[str, dict]:
    """card type → {capability, text, phase, inputs, outputs, acceptance} from EVERY capability
    YAML in ``<v4>/capabilities`` (R228), archetype inheritance applied per (list, slot)."""
    caps = v4 / "capabilities"
    files = sorted(caps.glob("*.yaml"))
    missing = [f for f in V4_REQUIRED_FILES if not (caps / f"{f}.yaml").exists()]
    if missing:
        raise SystemExit(f"{caps}: not a v4 design, missing {missing}")
    docs = {f.stem: yaml.safe_load(f.read_text()) or {} for f in files}
    declared: dict[str, list[str]] = {}
    # Pass 1: every card type a file declares (card_types or outputs) with its own entries.
    found: dict[str, tuple[str, dict]] = {}
    for stem, doc in docs.items():
        # R194: the explicit ``inputs:`` section is the only source of card inputs.
        for name, names in (doc.get("inputs") or {}).items():
            if name in declared:
                raise SystemExit(f"{name}: inputs declared in two capability files")
            if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
                raise SystemExit(f"{stem}.yaml inputs.{name}: not a list of names")
            declared[name] = list(names)
        for name in [*(doc.get("card_types") or {}), *(doc.get("outputs") or {})]:
            if name in found and found[name][0] != stem:
                raise SystemExit(f"{name}: defined in two capability files")
            found[name] = (stem, doc)
    # Pass 2: resolve archetype + inherited outputs; a type with no outputs is not defined.
    cards: dict[str, dict] = {}
    for name, (stem, doc) in found.items():
        entry = (doc.get("card_types") or {}).get(name) or {}
        cap = doc["capability"]
        own = (doc.get("outputs") or {}).get(name)
        arch = entry.get("archetype") or ARCHETYPE_OF_CAPABILITY.get(cap)
        base = ARCHETYPE_BASE.get(arch or "")
        if base and base != name and base in found:
            bstem, bdoc = found[base]
            own = _inherit(own, (bdoc.get("outputs") or {}).get(base))
        if own is None:
            continue  # no outputs design: not defined (R193 refuses it if placed)
        missing_lists = [lst for lst in LISTS if lst not in own]
        if missing_lists:
            raise SystemExit(f"{stem}.yaml {name}: outputs lack lists {missing_lists}")
        outputs = _dedup(
            [k for lst in LISTS for k in own[lst]["releasing"] if k not in OUTPUT_EXCLUDE]
        )
        acceptance = [
            f"{lst}: {check}" for lst in LISTS for check in own[lst]["inspecting"]["checks"]
        ]
        cards[name] = {
            "capability": CAPABILITY_ALIASES.get(cap, cap),
            "text": entry.get("text"),
            "phase": entry.get("phase"),
            "inputs": [],
            "outputs": outputs,
            "acceptance": acceptance,
        }
    undefined = sorted(set(declared) - set(cards))
    if undefined:
        raise SystemExit(f"inputs declared for card types with no outputs design: {undefined}")
    for name, names in declared.items():
        cards[name]["inputs"] = _dedup(names)
    return cards


def it_matrix(v4_cards: dict[str, dict]) -> dict[str, list[str]]:
    """R228: ``it`` places every v4-defined card type (see IT_PM_MATRIX above).

    ⛔ Except the KATA-ONLY types (``KATA_CARD_TYPES``, George 2026-11-01, Kata WP4): an ordinary
    ``it`` project's matrix is exactly what it was before they were designed.

    Plus the rewrite chain's three held types in ``execution`` (``IT_REWRITE_IN``, George
    2026-11-07): they are not in the v4 YAML, so the loop above never sees them.
    """
    matrix = {p: list(v) for p, v in IT_PM_MATRIX.items()}
    pm = {n for v in IT_PM_MATRIX.values() for n in v}
    for name, card in v4_cards.items():
        if name in pm or name in KATA_CARD_TYPES:
            continue
        phase = (
            IT_PHASE_OF_TYPE.get(name)
            or IT_PHASE_OF_CAPABILITY.get(card["capability"])
            or card["phase"]
            or IT_PHASE_OF_UNPHASED.get(name)
        )
        if phase is None:
            raise SystemExit(f"{name}: v4-defined card type with no phase (R228 places every one)")
        if phase not in matrix or phase == "setup":
            raise SystemExit(f"{name}: phase {phase!r} is not a work phase of {IT_TYPE}")
        if (
            card["phase"]
            and name in IT_PHASE_OF_UNPHASED
            and card["phase"] != IT_PHASE_OF_UNPHASED[name]
        ):
            raise SystemExit(f"{name}: design phase {card['phase']!r} != R185 placement")
        matrix[phase].append(name)
    for phase, names in IT_REWRITE_IN.items():
        for name in names:
            if any(name in v for v in matrix.values()):
                raise SystemExit(f"{name}: already placed; drop it from IT_REWRITE_IN")
            matrix[phase].append(name)
    for phase, names in IT_ALSO_IN.items():
        for name in names:
            if not any(name in v for v in matrix.values()):
                raise SystemExit(f"{name}: second placement in {phase!r} of an unplaced type")
            matrix[phase].append(name)
    return matrix


# ---------------------------------------------------------------------------
# Tier 2 constants
# ---------------------------------------------------------------------------

# A project-type-wide WIP limit of 1 for every default project type (the plan's ruling:
# "project total 1 per project type"; fr's local/conveyor ``implementation`` → 1 is
# obsolete and not copied).
DEFAULT_WIP_LIMIT = 1

# fr ``backend/src/services/improvement-cards/request-schema.ts`` CARD_PRIORITIES
# (P0..P3, rank = index, P0 highest) and ``backend/src/lib/priority-change.ts``
# assertConveyorNotP0 / ``lib/conveyor-identity.ts`` (the conveyor may never set P0).
# P2 is the default (plan ruling). Names are lower snake (catalogue rule); the title
# carries fr's spelling.
PRIORITY_LEVELS: list[dict] = [
    {
        "name": f"p{rank}",
        "title": f"P{rank}",
        "description": desc,
        "rank": rank,
        "is_default": rank == 2,
        "conveyor_may_set": rank != 0,
    }
    for rank, desc in enumerate(
        (
            "Highest priority; set by humans only.",
            "High priority.",
            "Normal priority; the default.",
            "Low priority.",
        )
    )
]

# Plan ruling: both kinds count towards their phase's exit.
DELIVERABLE_KINDS: list[dict] = [
    {
        "name": "deliverable",
        "title": "Deliverable",
        "description": "A result the project hands over.",
        "counts_for_phase_exit": True,
    },
    {
        "name": "intermediate",
        "title": "Intermediate",
        "description": "A result a later card in the project consumes.",
        "counts_for_phase_exit": True,
    },
]

# One milestone per work-phase exit (R185; George 2026-11-03: preliminary, analysis, design,
# execution, test). v3's other four
# (brief_accepted/request, contract_signed/contract, validated/validation,
# released/release) went with their phases; setup has no milestone, as in v3.
MILESTONE_KINDS: list[dict] = [
    {
        "name": name,
        "title": _title(name),
        "description": f"The {phase} phase is done.",
        "phase": phase,
    }
    for name, phase in (
        ("preliminary_done", "preliminary"),
        ("analysis_done", "analysis"),
        ("design_done", "design"),
        ("executed", "execution"),
        ("tested", "test"),
    )
]

# v3's one gate (``contract_signoff``, phases.yaml contract.gate) is gone with the
# ``contract`` phase (R185). v4 sign-offs are fr approvals asked by the cards themselves
# (brief: project_setup QA; plan: project_phase_start QA; phase: project_phase_closure QA;
# project: project_closure QA; merge: development_bug_fix DONE), not RM gates.
GATES: list[dict] = []

# fr ``backend/src/services/planka/intake.ts`` DEFAULT_LIST_NAMES; DONE and DROPPED are the
# terminal (closed) lists.
BOARD_LISTS = ("TODO", "READY", "DOING", "QA", "BLOCKED", "DONE", "DROPPED")
CLOSED_LISTS = {"DONE", "DROPPED"}

# The board template's custom-field groups. ⛔ RM's ``default`` template is the MASTER (George
# 10-13 v4 ruling 12 / R46): fr builds every board exactly from it and the conveyor reads it;
# nothing here follows fr any more. Seeded once from fr
# ``backend/src/services/improvement-cards/field-groups.ts`` GROUP_FIELDS @ de6c7763 (9
# groups, Metadata first, Runtime State last; retired ``stopwatch_timeout`` left out), then:
# D4 adds ``project_type`` / ``phase`` / ``capability`` right after ``card_type`` in Metadata;
# ruling 10 adds ``released`` to Runtime State (R66: no ``rework_count``; rework counting
# is not a concept); R188 adds ``source_artifact_id`` / ``source_item_id`` at the end of
# Metadata (set at creation, read by the card's flow as fixed ids); R190 adds the closure
# wait flag ``wait_for_phase`` (bool) at the end of Runtime State; R311 adds ``spec_hash`` at the
# end of Metadata; R-CK10 adds ``criterion_ids`` at the end of Relations.
RETIRED_FIELDS = frozenset({"stopwatch_timeout"})
_GROUP_FIELDS: list[tuple[str, list[str]]] = [
    (
        "Metadata",
        [
            "work_type",
            "card_group",
            "priority",
            "scope_level",
            "requestor",
            "creator_role",
            "stopwatch_timeout",
            "project_id",
            "constraints",
            "stream_id",
            "stream_slug",
            "card_type",
            "project_type",  # D4
            "phase",  # D4
            "capability",  # D4
            "asset_id",
            "asset_kind",
            "detector_severity",
            "manual_trigger",
            "worker_name",
            "keycloak_tenant_id",
            "user_id",
            "subtype",
            "authored_asset_id",
            "version_stamp",
            "plan_pin",
            "source_artifact_id",  # R188
            "source_item_id",  # R188
            "spec_hash",  # R311: the card birth key
        ],
    ),
    # R-CK10: ``criterion_ids`` = the brief acceptance-criterion ids a card serves (a JSON or
    # comma list, like ``required_by``), read by name by ct.project_closure.doing.
    ("Relations", ["parent_card", "child_cards", "requires", "required_by", "criterion_ids"]),
    ("Acceptance Criteria", [f"criterion_{i}" for i in range(1, 6)]),
    ("Validation", ["scenario_titles", "gherkin_summary", "qa_exit_rule", "blocked_conditions"]),
    ("Requirements", [f"requirement_{i}" for i in range(1, 6)]),
    ("Principles", [f"principle_{i}" for i in range(1, 6)]),
    ("Decisions", [f"decision_{i}" for i in range(1, 6)]),
    ("Procedures", ["readying_procedure", "doing_procedure", "qa_procedure", "done_procedure"]),
    (
        "Runtime State",
        [
            "prefetch_results",
            "counts",
            "sparse_dimensions",
            "expansion",
            "keywords_used",
            "output_summary",
            "step_results",
            "container_name",
            "status",
            "artifact_id",
            "reasoning_verdict",
            "execution_tier",
            "procedure_used",
            "deliverables",
            "pr_url",
            "pr_number",
            "pr_repo_owner",
            "pr_repo_name",
            "pr_is_draft",
            "verdict",
            "recommendation",
            "issues",
            "qa_exit_rule_applied",
            "oracle_tier",
            "blocking_gate",
            "escalate",
            "escalation_reason",
            "decision_id",
            "principles_applied",
            "completion_summary",
            "kaizen_log_id",
            "finalized",
            "notification_status",
            "block_reason",
            "old_workflow_id",
            "new_workflow_id",
            "proposal",
            "is_analysis_only",
            "phase_run_id",
            "validation_ok",
            "validation_violations",
            "commit_sha",
            "qa_chain",
            "acs_count",
            "cause",
            "split_request",
            "wip_slot_id",
            "priority_set_by",
            "priority_base",
            "job_pending",
            "context_pack_ref",
            "context_pack_output_ref",
            "context_pack_qa_output_ref",
            "check_json_sha256",
            "analysis_report",
            "hunt_result_id",
            "grid_handover_ready",
            "grid_handover_doing",
            "released",  # ruling 10
            # R190: the closure wait flag, a bool. A field group carries names only (RM's
            # template has no field-type column), so the bool type lives in this comment
            # and in R190, not in the data.
            "wait_for_phase",
        ],
    ),
]
CUSTOM_FIELD_GROUPS: list[dict] = [
    {"name": name, "fields": [f for f in fields if f not in RETIRED_FIELDS]}
    for name, fields in _GROUP_FIELDS
]

# Default card-type rules (bare rule names, in this order). A target the catalogue does not
# carry (not in the registry, or not placed by a default project type) is skipped and listed
# in ``sources.skipped_rule_targets``.
CARD_TYPE_RULES: dict[str, tuple[str, ...]] = {
    # repo_write / container_read_exec / setup: n8n_custom_nodes origin/main @ bd97bbede
    # ``AA_IMPLEMENTATION/coding_card_types.json`` (the coding card-type single source,
    # movies-64): DOING writes the repo / reads+executes in the workspace / creates it.
    "repo_write": (
        "architecture_refactoring",
        "bdd_test_development",
        "bdd_test_improvement",
        "code_smell_refactoring",
        "decoupling_refactoring",
        "dependency_refactoring",
        "development_bug_fix",
        "development_chore",
        "development_feature_new",
        "development_feature_update",
        "development_iac_security_patch",
        "development_poc",
        "development_security_patch",
        "development_source_security_patch",
        "function_class_reallocation",
        "library_refactoring",
        "optimization_refactoring",
        "re_engineering",
        "tdd_test_development",
        "tdd_test_improvement",
        "test_development",
    ),
    "container_read_exec": (
        "bdd_test_evaluation",
        "bug_analysis",
        "security_analysis",
        "structure_analysis",
        "tdd_test_evaluation",
        "testing_execution",
        "verification_test",
    ),
    "setup": ("project_setup",),
    # no_reclassify_target: the set given in the fr computed-gating brief (2026-10-13);
    # fr feat/fr-rm-pm-catalogue reads card-type rules from RM.
    "no_reclassify_target": (
        "bug_hunt_sweep",
        "bug_analysis",
        "development_chore",
        "security_analysis",
        "structure_analysis",
        "verification_test",
    ),
    # dependency_upgrade: fr field-groups.ts Metadata ``subtype`` (OD-6) — the
    # project-chain spawner stamps ``subtype:"dependency_upgrade"`` on development_chore.
    "dependency_upgrade": ("development_chore",),
}


BOARD_TEMPLATES: list[dict] = [
    {
        "name": "default",
        "title": "Default board",
        "description": "The board every project starts with.",
        "project_type": None,
        "lists": [
            {"name": n, "position": i, "closed": n in CLOSED_LISTS}
            for i, n in enumerate(BOARD_LISTS)
        ],
        # No source names a colour for TEMPLATE; null = not specified.
        "labels": [{"name": "TEMPLATE", "color": None}],
        "custom_field_groups": CUSTOM_FIELD_GROUPS,
    }
]


def _tier2(project_types: list[dict], phase_names: list[str]) -> dict:
    for row in (*MILESTONE_KINDS, *GATES):
        if row["phase"] not in phase_names:
            raise SystemExit(f"{row['name']}: phase {row['phase']!r} is not a default phase")
    return {
        "wip_limits": [
            {
                "project_type": pt["name"],
                "phase": None,
                "capability": None,
                "card_type": None,
                "wip_limit": DEFAULT_WIP_LIMIT,
            }
            for pt in project_types
        ],
        "priority_levels": PRIORITY_LEVELS,
        "deliverable_kinds": DELIVERABLE_KINDS,
        "milestone_kinds": MILESTONE_KINDS,
        "gates": GATES,
        "board_templates": BOARD_TEMPLATES,
    }


def build(registry: Path, v4: Path, predicates: Path) -> dict:
    project_types, capabilities, card_types = parse_registry(registry.read_text())
    # v4 ruling 9: the second source. Never re-defines a registry row.
    clash = {c["name"] for c in V4_CAPABILITIES} & {c["name"] for c in capabilities} | (
        {c["name"] for c in V4_CARD_TYPES} & {c["name"] for c in card_types}
    )
    if clash:
        raise SystemExit(f"v4 additions already in the registry: {sorted(clash)}")
    capabilities = [*capabilities, *(dict(c) for c in V4_CAPABILITIES)]
    cap_names = {c["name"] for c in capabilities}
    for c in V4_CARD_TYPES:
        if c["capability"] not in cap_names:
            raise SystemExit(f"{c['name']}: unknown capability {c['capability']!r}")
    card_types = [*card_types, *(dict(c) for c in V4_CARD_TYPES)]
    by_name = {c["name"]: c for c in card_types}
    phase_names = [p["name"] for p in V4_PHASES]  # the it spine

    # R184: ``it`` is the registry's row; R185 its phases + matrix; R88 its entry.
    if IT_TYPE not in {pt["name"] for pt in project_types}:
        raise SystemExit(f"registry has no {IT_TYPE!r} project type")
    v4_cards = parse_v4(v4)
    held = sorted(set(REWRITE_CARD_TYPE_DEFINITIONS) & set(v4_cards))
    if held:
        raise SystemExit(f"now defined by the v4 YAML, drop the held definitions: {held}")
    matrix = it_matrix(v4_cards)
    if list(matrix) != phase_names or list(IT_PHASES) != phase_names:
        raise SystemExit(f"{IT_TYPE} phases {list(matrix)} != default phases {phase_names}")
    for phase, names in IT_ENTRY.items():
        if not set(names) <= set(matrix[phase]):
            raise SystemExit(f"{IT_TYPE}/{phase}: entry {names} not placed in the phase")
    out_types = []
    for pt in project_types:
        if pt["name"] == IT_TYPE:
            out_types.append(
                {
                    **pt,
                    "phases": list(matrix),
                    "card_types": {p: sorted(v) for p, v in matrix.items()},
                    "entry_card_types": {p: sorted(v) for p, v in IT_ENTRY.items()},
                }
            )
        else:
            # ⛔ V4 SCOPE: program, meta_prompt, meta_procedure and kaizen stay EXACTLY as
            # before v4 — no phases, no card types — so creating one is refused (409
            # ``project_type_missing_setup_phase``) until a ruling brings them in.
            out_types.append({**pt, "phases": [], "card_types": {}, "entry_card_types": {}})

    # Kata: v4 addition (see KATA_TYPE). Never re-defines a registry row.
    if KATA_TYPE["name"] in {pt["name"] for pt in project_types}:
        raise SystemExit(f"v4 addition already in the registry: {KATA_TYPE['name']!r}")
    # Kata keeps its own phases: each a default phase, in the default order.
    if [p for p in phase_names if p in KATA_MATRIX] != list(KATA_MATRIX):
        raise SystemExit(f"kata phases {list(KATA_MATRIX)} not in default order {phase_names}")
    out_types.append(
        {
            **KATA_TYPE,
            "phases": list(KATA_MATRIX),
            "card_types": {p: sorted(v) for p, v in KATA_MATRIX.items()},
            "entry_card_types": {
                p: sorted(n for n in v if n in KATA_MATRIX.get(p, []))
                for p, v in IT_ENTRY.items()
                if any(n in KATA_MATRIX.get(p, []) for n in v)
            },
        }
    )

    # The rewrite chain's held definitions (see REWRITE_CARD_TYPE_DEFINITIONS): `it` places the
    # three in execution (IT_REWRITE_IN), so they must be in v4_cards before the R193 check.
    v4_cards.update({n: dict(v) for n, v in REWRITE_CARD_TYPE_DEFINITIONS.items()})

    # Only the card types a default project type places (the non-IT types place none).
    placed = {n for pt in out_types for names in pt["card_types"].values() for n in names}
    unknown = sorted(placed - set(by_name))
    if unknown:
        raise SystemExit(f"placed card types missing from the registry: {unknown}")
    unplaced = len(card_types) - len(placed)
    card_types = [c for c in card_types if c["name"] in placed]

    # R193: every placed card type is defined by the v4 capability YAML.
    undefined = sorted(placed - set(v4_cards))
    if undefined:
        raise SystemExit(f"placed card types with no v4 definition: {undefined}")
    dod = json.loads(predicates.read_text())["card_types"]
    for c in card_types:
        v = v4_cards[c["name"]]
        # capabilities: the registry is the only source; the YAML must agree, never re-map.
        if v["capability"] != c["capability"]:
            raise SystemExit(
                f"{c['name']}: v4 capability {v['capability']!r} != registry {c['capability']!r}"
            )
        # The registry tags rows it added with "(added <date> from the roster)"; the design's
        # `text` quotes the row without that provenance note, so it is ignored when comparing.
        if v["text"] is not None and " ".join(v["text"].split()) != ADDED_NOTE.sub(
            "", c["description"]
        ):
            raise SystemExit(f"{c['name']}: v4 text differs from the registry description")
        c["inputs"] = v["inputs"]
        c["outputs"] = v["outputs"]
        c["acceptance"] = v["acceptance"]
        c["rules"] = [rule for rule, targets in CARD_TYPE_RULES.items() if c["name"] in targets]
        c["human_task"] = c["name"] in HUMAN_TASKS
        c["definition_of_done"] = dod.get(c["name"])
        if c["name"] in REWRITE_CARD_TYPE_DEFINITIONS:  # held with the live row's own values
            c["rules"] = list(v["rules"])
            c["definition_of_done"] = v["definition_of_done"]

    known = {c["name"] for c in card_types}
    skipped_rule_targets = {
        rule: sorted(set(targets) - known)
        for rule, targets in CARD_TYPE_RULES.items()
        if set(targets) - known
    }

    return {
        "sources": {
            "registry": (
                "n8n_custom_nodes/AA_IMPLEMENTATION/trigger_registry.md @ d469ca1c6 "
                "+ the 4 py2rust rewrite_* card types added 2026-10-26 (RS-D4, George 10-25) "
                "(vendored: scripts/sources/trigger_registry.md)"
            ),
            "card_type_definitions": (
                "n8n-designs/design/choices/v4/capabilities/*.yaml (R193, R228: every file; "
                "archetype outputs inherited per (list, slot) from the reference type): inputs = the "
                "explicit `inputs:` section only (R194; [] when absent); outputs = every releasing key "
                "(minus handoff_comment_id); acceptance = every inspecting check as "
                "'<LIST>: <check>'"
            ),
            "definition_of_done": (
                "n8n_custom_nodes/AA_IMPLEMENTATION/landing_predicates.json @ e21ee10f0 "
                "(vendored: scripts/sources/landing_predicates.json)"
            ),
            "name_fixes": NAME_FIXES,
            "human_tasks": sorted(HUMAN_TASKS),
            "unplaced_card_types": unplaced,
            "priority_levels": (
                "fr backend/src/services/improvement-cards/request-schema.ts CARD_PRIORITIES; "
                "backend/src/lib/priority-change.ts assertConveyorNotP0"
            ),
            "board_lists": "fr backend/src/services/planka/intake.ts DEFAULT_LIST_NAMES",
            "custom_field_groups": (
                "RM master (George 10-13 v4 ruling 12): seeded from fr "
                "backend/src/services/improvement-cards/field-groups.ts GROUP_FIELDS "
                "@ feat/fr-rm-pm-catalogue de6c7763 (retired stopwatch_timeout excluded); "
                "+ project_type, phase, capability (D4); + released (ruling 10; R66: no "
                "rework_count); + source_artifact_id, source_item_id (R188); + wait_for_phase "
                "(bool, R190); + spec_hash in Metadata (R311: the card birth key, "
                "kanban-coach-errors.md §7 item 1); + criterion_ids in Relations (R-CK10: "
                "the acceptance-criterion ids a card serves, a JSON or comma list like "
                "required_by, read by name by ct.project_closure.doing)"
            ),
            "v4_rulings": {
                "scope": (
                    "George 10-13 R184: one IT project type `it` (replaces bug_hunt_fix, "
                    "feature_new, feature_update, integration_report); program, meta_prompt, "
                    "meta_procedure, kaizen unchanged; George 2026-10-31 Kata K15: the `kata` "
                    "type, with the first-build phases of `it` (setup, preliminary, execution), "
                    "the setup card types of `it` and (Kata WP4, "
                    "George 2026-11-01) the four kata-only preliminary workflow_engineering card "
                    "types, plus (Kata WP5, George 2026-11-03) the execution-phase wf_change"
                ),
                "phases": (
                    "R185; George 2026-11-03: it = setup -> preliminary -> analysis -> design "
                    "-> execution -> test (closure is fr's, not RM's)"
                ),
                "setup_phase": "George 10-13 v4 ruling 2 (D16)",
                "matrix": (
                    "R185 phases; R228 (George 10-14): it places every v4-defined card type; "
                    "George 2026-11-03 by capability: analysis -> analysis; architecture, design, "
                    "bdd_tdd -> design; testing -> test; choice_option_development (R331) and "
                    "decision_development (its upstream) -> execution; every other type in its "
                    "design `phase` (development_bug_fix execution); PM types fixed "
                    "(project_closure in test); entry card type project_start_request_analysis "
                    "(R88); EXCEPT the KATA-ONLY workflow_engineering types (Kata WP4, George "
                    "2026-11-01: wf_observe, wf_probe, wf_root_cause, wf_andon_scope; Kata WP5, "
                    "George 2026-11-03: wf_change), which are placed on `kata` alone"
                ),
                "additions": (
                    "George 10-13 v4 ruling 9: capability kanban_coach; card types "
                    "project_phase_start, project_phase_closure (project_management), text "
                    "from n8n-designs design/choices/v4 (kanban_coach: model.md, first designed "
                    "in capabilities/kanban_coach.yaml @ afcf981; project_management.yaml); "
                    "phase_coverage_check: retired by R347 (4)"
                ),
                "capabilities": [c["name"] for c in V4_CAPABILITIES],
                "card_types": [c["name"] for c in V4_CARD_TYPES],
            },
            "card_type_rules": (
                "n8n_custom_nodes AA_IMPLEMENTATION/coding_card_types.json @ bd97bbede "
                "(repo_write, container_read_exec, setup); no_reclassify_target and "
                "dependency_upgrade per the fr computed-gating brief"
            ),
            "skipped_rule_targets": skipped_rule_targets,
        },
        "project_phases": [dict(p) for p in V4_PHASES],
        "capabilities": capabilities,
        "card_types": card_types,
        "project_types": out_types,
        "recipes": [],
        **_tier2(out_types, phase_names),
    }


def shrink_error(out: Path, built: dict) -> str | None:
    """The refusal message if ``built`` places fewer card types than ``out`` holds, else None.

    A design checkout missing the full v4 set (e.g. one without R228's types) would otherwise
    regenerate a smaller catalogue and exit 0, silently dropping card types.
    """
    if not out.exists():
        return None
    old = [c["name"] for c in json.loads(out.read_text()).get("card_types", [])]
    new = {c["name"] for c in built["card_types"]}
    if len(new) >= len(old):
        return None
    dropped = sorted(set(old) - new)
    return (
        f"refusing to shrink {out}: it places {len(old)} card types, the sources place "
        f"{len(new)}; dropped: {dropped}. Is --v4 / $PM_CATALOGUE_V4_DIR the full design? "
        "Pass --allow-shrink if the drop is intended."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--v4", type=Path, default=DEFAULT_V4)
    ap.add_argument("--predicates", type=Path, default=DEFAULT_PREDICATES)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--allow-shrink", action="store_true")
    args = ap.parse_args()
    built = build(args.registry, args.v4, args.predicates)
    rendered = json.dumps(built, indent=2, ensure_ascii=False) + "\n"
    shrink = None if args.allow_shrink else shrink_error(args.out, built)
    if shrink:
        print(shrink, file=sys.stderr)
        return 1
    if args.check:
        current = args.out.read_text() if args.out.exists() else ""
        if current != rendered:
            print(f"{args.out} is stale; re-run without --check", file=sys.stderr)
            return 1
        print("up to date")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered)
    d = json.loads(rendered)
    print(
        f"wrote {args.out}: {len(d['project_phases'])} phases, {len(d['capabilities'])} "
        f"capabilities, {len(d['card_types'])} card types, {len(d['project_types'])} project types, "
        f"{len(d['recipes'])} recipes, {len(d['wip_limits'])} WIP limits, "
        f"{len(d['priority_levels'])} priority levels, {len(d['milestone_kinds'])} milestone kinds"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

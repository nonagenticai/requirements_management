"""scripts/build_pm_catalogue_defaults.py: R228 placement and R193 refusal, on a synthetic design.

The committed JSON is pinned by test_pm_v4_rulings / test_pm_catalogue; these pin the builder's
rules themselves, so a builder that silently drops or misplaces a type goes red even when the
JSON is not regenerated.
"""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "build_pm_catalogue_defaults", SCRIPTS / "build_pm_catalogue_defaults.py"
)
assert _spec and _spec.loader
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

LISTS = ("READY", "DOING", "QA", "DONE")


def _outputs(tag: str) -> dict:
    return {
        lst: {
            "releasing": {f"{tag}_{lst.lower()}_id": "str", "handoff_comment_id": "str"},
            "inspecting": {"checks": [f"{tag}_ok"]},
        }
        for lst in LISTS
    }


def _write(design: Path, stem: str, doc: dict) -> None:
    caps = design / "capabilities"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / f"{stem}.yaml").write_text(yaml.safe_dump(doc))


PM_TYPES = (
    "project_start_request_analysis",
    "project_setup",
    "project_phase_start",
    "project_phase_closure",
    "project_closure",
)


def _design(tmp_path: Path, *, pm=PM_TYPES, research_phase="preliminary") -> Path:
    _write(
        tmp_path,
        "project_management",
        {
            "capability": "project_management",
            "card_types": {n: {"text": None} for n in pm},
            "outputs": {n: _outputs(n) for n in pm},
        },
    )
    _write(
        tmp_path,
        "analysis",
        # R333: rewrite_plan is a v4-defined card type, so ``it`` places it by capability
        # (analysis), like the real design.
        {
            "capability": "analysis",
            "outputs": {"bug_analysis": _outputs("bug"), "rewrite_plan": _outputs("plan")},
        },
    )
    _write(
        tmp_path,
        "development",
        {
            "capability": "development",
            "card_types": {"development_poc": {"phase": "preliminary"}},
            "outputs": {"development_bug_fix": _outputs("fix")},
        },
    )
    # Not one of the three files the builder used to hard-code (R228: every file is read).
    research = {"phase": research_phase} if research_phase else {}
    _write(
        tmp_path,
        "research",
        {
            "capability": "research",
            "card_types": {
                "research_spike": research,
                "feasibility_study": {"phase": "preliminary"},
            },
            "outputs": {"research_spike": _outputs("spike")},
        },
    )
    _write(
        tmp_path,
        "bddtdd",
        {
            "capability": "bddtdd",
            "card_types": {
                "tdd_test_improvement": {"archetype": "development", "phase": "execution"}
            },
        },
    )
    # George 2026-11-03 "move by capability": architecture → design (except
    # choice_option_development, R331, and decision_development, its downstream), testing → test.
    _write(
        tmp_path,
        "architecture",
        {
            "capability": "architecture",
            "card_types": {
                "architecture_spike": {"phase": "preliminary"},
                "choice_option_development": {"phase": "execution"},
                "decision_development": {"phase": "preliminary"},
            },
        },
    )
    _write(
        tmp_path,
        "testing",
        {
            "capability": "testing",
            "card_types": {"testing_strategy": {"archetype": "document", "phase": "preliminary"}},
        },
    )
    # Kata WP4 (George 2026-11-01) + WP5 (2026-11-03): ``KATA_MATRIX`` always places the kata-only
    # types, so every design the builder accepts must define them (R193 refuses one that does not).
    kata = sorted(builder.KATA_CARD_TYPES)
    _write(
        tmp_path,
        "workflow_engineering",
        {
            "capability": "workflow_engineering",
            "card_types": {
                n: {"phase": "execution" if n in builder.KATA_EXECUTION else "preliminary"}
                for n in kata
            },
            "outputs": {n: _outputs(n) for n in kata},
        },
    )
    return tmp_path


def test_every_file_is_read_and_siblings_inherit_outputs(tmp_path: Path):
    cards = builder.parse_v4(_design(tmp_path))
    # feasibility_study has no outputs entry: inherits research_spike's (document archetype).
    assert cards["feasibility_study"]["outputs"] == cards["research_spike"]["outputs"]
    # development_poc / tdd_test_improvement inherit development_bug_fix's.
    assert cards["development_poc"]["outputs"] == cards["development_bug_fix"]["outputs"]
    assert cards["tdd_test_improvement"]["capability"] == "bdd_tdd", "registry spelling"
    assert "handoff_comment_id" not in cards["research_spike"]["outputs"]


def test_matrix_places_every_defined_type_by_capability_then_design_phase(tmp_path: Path):
    m = builder.it_matrix(builder.parse_v4(_design(tmp_path)))
    assert list(m) == ["setup", "preliminary", "analysis", "design", "execution", "test"]
    assert m["setup"] == ["project_start_request_analysis", "project_setup"]
    pm = {"project_phase_start", "project_phase_closure"}  # phase_coverage_check: R347 (4)
    # research / development keep their design phase.
    assert set(m["preliminary"]) == pm | {"development_poc", "research_spike", "feasibility_study"}
    # George 2026-11-03: by capability, overriding the design phase.
    assert set(m["analysis"]) == pm | {"bug_analysis", "rewrite_plan"}
    assert set(m["design"]) == pm | {"architecture_spike", "tdd_test_improvement"}
    # George 2026-11-07: the rewrite chain lives on `it` — its three held types sit in
    # execution (IT_REWRITE_IN); no py2rust project type exists.
    assert set(m["execution"]) == pm | {
        "development_bug_fix",
        "choice_option_development",
        "decision_development",
        *builder.REWRITE_CARD_TYPE_DEFINITIONS,
    }
    # project_closure sits in the last phase (R88); development_bug_fix is ALSO placed there.
    assert set(m["test"]) == pm | {"project_closure", "testing_strategy", "development_bug_fix"}
    # ⛔ The kata-only types are defined by the design and placed on kata alone (Kata WP4).
    assert not {n for v in m.values() for n in v} & builder.KATA_CARD_TYPES


def test_a_defined_type_with_no_phase_is_refused(tmp_path: Path):
    cards = builder.parse_v4(_design(tmp_path, research_phase=None))
    with pytest.raises(SystemExit, match="research_spike: v4-defined card type with no phase"):
        builder.it_matrix(cards)


def test_a_missing_required_file_is_refused(tmp_path: Path):
    (_design(tmp_path) / "capabilities" / "analysis.yaml").unlink()
    with pytest.raises(SystemExit, match="not a v4 design"):
        builder.parse_v4(tmp_path)


def test_r193_a_placed_type_without_a_definition_is_refused(tmp_path: Path):
    design = _design(tmp_path, pm=PM_TYPES[:-1])  # project_closure has no design
    with pytest.raises(SystemExit, match=r"no v4 definition: \['project_closure'\]"):
        builder.build(builder.DEFAULT_REGISTRY, design, builder.DEFAULT_PREDICATES)


def _run_main(monkeypatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["build_pm_catalogue_defaults.py", *args])
    return builder.main()


def test_a_smaller_design_is_refused_without_allow_shrink(tmp_path: Path, monkeypatch, capsys):
    out = tmp_path / "defaults.json"
    committed = builder.DEFAULT_OUT.read_text()
    out.write_text(committed)
    design = _design(tmp_path / "design")
    assert _run_main(monkeypatch, "--v4", str(design), "--out", str(out)) == 1
    err = capsys.readouterr().err
    assert "refusing to shrink" in err and "dropped:" in err and "the sources place" in err
    assert out.read_text() == committed, "nothing written"
    assert _run_main(monkeypatch, "--v4", str(design), "--out", str(out), "--check") == 1
    assert "refusing to shrink" in capsys.readouterr().err


def test_allow_shrink_permits_a_smaller_design(tmp_path: Path, monkeypatch):
    out = tmp_path / "defaults.json"
    out.write_text(builder.DEFAULT_OUT.read_text())
    design = _design(tmp_path / "design")
    assert _run_main(monkeypatch, "--v4", str(design), "--out", str(out), "--allow-shrink") == 0
    full = len(json.loads(builder.DEFAULT_OUT.read_text())["card_types"])
    assert 0 < len(json.loads(out.read_text())["card_types"]) < full

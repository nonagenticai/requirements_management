"""Pins the request vocabulary against the filter table that actually answers.

``tools/architecture/view_generator.generate_view`` does::

    filters = VIEWPOINT_FILTERS.get(vp)
    if filters is None:
        return {"viewpoint": vp, "element_ids": [], "relationship_ids": [],
                "element_count": 0, "relationship_count": 0}

That branch produces a **200 carrying an empty view**, which is
indistinguishable on the wire from "this viewpoint legitimately matches nothing
in your model". Today the branch is unreachable, because the 22-member
``ArchitectureViewpoint`` Literal and the 22 keys of ``VIEWPOINT_FILTERS`` are
identical and the Literal rejects everything else at the request boundary.

That equality is a coincidence of maintenance, not a mechanism. Adding a member
to the Literal (or to ``ARCHITECTURE_VIEWPOINTS``, which the Literal mirrors)
without adding the matching filter entry re-arms the branch: the new value
passes validation, reaches ``generate_view``, misses the table and answers an
empty view with 200. Nothing else in the repo notices — an empty view has no
error, no log line, and a plausible-looking shape.

So this file pins the two sides set-equal and names BOTH directions of any
difference, because they are different defects:

* in the Literal, missing from the table -> the silent empty 200 above;
* in the table, missing from the Literal -> a filter nobody can reach, i.e. a
  viewpoint the API refuses with 422 while the engine can render it.

⛔ Deliberately NOT a source change. Making ``generate_view`` raise on an
unknown viewpoint would change a pure, framework-agnostic function's contract
for a case its own callers cannot produce, and ``tests/test_architecture.py``
pins the current return shape. A test is the honest fix: it fails at the moment
the drift is INTRODUCED, in the diff that introduces it, rather than at the
moment a caller is quietly lied to in production.
"""

from typing import get_args

from schemas.tools import ArchitectureViewpoint
from tools.architecture.view_generator import VIEWPOINT_FILTERS


def test_every_declared_viewpoint_has_a_filter_and_vice_versa() -> None:
    """KILLING MUTATION: append a member to the ``ArchitectureViewpoint``
    Literal without adding the same key to ``VIEWPOINT_FILTERS`` -> RED.

    SECOND KILLING MUTATION: add a key to ``VIEWPOINT_FILTERS`` that the
    Literal does not declare -> RED, naming the other side.
    """
    declared = set(get_args(ArchitectureViewpoint))
    filtered = set(VIEWPOINT_FILTERS)

    # Vacuity floor: an empty Literal (or an empty table) would make the
    # set-equality below trivially true. Both sides must be populated for the
    # comparison to mean anything.
    assert declared, "ArchitectureViewpoint declares no members"
    assert filtered, "VIEWPOINT_FILTERS is empty"

    accepted_but_unfiltered = sorted(declared - filtered)
    filtered_but_unreachable = sorted(filtered - declared)

    assert declared == filtered, (
        "ArchitectureViewpoint and VIEWPOINT_FILTERS have drifted apart.\n"
        f"  Accepted by the request schema but ABSENT from VIEWPOINT_FILTERS "
        f"(these answer an empty view with HTTP 200): {accepted_but_unfiltered}\n"
        f"  Present in VIEWPOINT_FILTERS but NOT accepted by the request schema "
        f"(unreachable filters; the API 422s them): {filtered_but_unreachable}\n"
        "Add the missing entry to whichever side lacks it — do not delete from "
        "the other."
    )


def test_every_heatmap_dimension_the_schema_accepts_is_one_the_scorer_computes() -> None:
    """The same drift guard, for the OTHER vocabulary closed in this change.

    ⛔ WHY THIS EXISTS SEPARATELY. ``HeatmapDimension`` (schemas/tools.py) decides
    what the API *accepts*; ``build_capability_heatmap`` decides what it can
    actually *score*, via three literal ``if "<dim>" in dims`` branches whose
    names are mirrored in ``DEFAULT_DIMENSIONS``. Nothing connected the two, so
    the vocabularies could drift apart in either direction and every existing
    test stayed green:

    * a name added to the scorer but not the Literal -> the API 422s a dimension
      the engine computes perfectly well;
    * a name added to the Literal but not the scorer -> the caller gets 200 with
      the key ABSENT from each capability's ``scores`` and present in
      ``averages`` as ``0.0``. That is the "confidently wrong 200" the closed
      Literal was introduced to kill, walking straight back in.

    The second direction is not hypothetical: it is exactly what
    ``dimensions=["bogus"]`` did in production before this change.
    """
    from typing import get_args

    from schemas.tools import HeatmapDimension
    from tools.architecture.capability_heatmap import DEFAULT_DIMENSIONS

    accepted = set(get_args(HeatmapDimension))
    scored = set(DEFAULT_DIMENSIONS)

    # Vacuity floor on both sides: two empty sets are equal, and would make this
    # test pass while proving nothing at all.
    assert accepted, "HeatmapDimension declares no members"
    assert scored, "DEFAULT_DIMENSIONS is empty"

    assert accepted == scored, (
        "the heatmap vocabularies have drifted apart:\n"
        f"  Accepted by the request schema but NOT scored by the engine "
        f"(200 with an empty `scores` and a fake 0.0 average): {sorted(accepted - scored)}\n"
        f"  Scored by the engine but REJECTED by the request schema "
        f"(422 on a dimension that works): {sorted(scored - accepted)}\n"
        "Add the missing name to whichever side lacks it — do not delete from the other."
    )

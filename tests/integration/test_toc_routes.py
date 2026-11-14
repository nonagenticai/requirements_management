"""Integration coverage for the /toc list + update routes.

Three routes that no test in this repo had ever called: ``GET /toc``,
``PUT /toc/{tree_id}`` and ``PUT /toc/{tree_id}/nodes/{node_id}``.

The headline case is the list filter's case asymmetry. ``TocTreeIn.tree_type``
normalises the request BODY (``_normalise_vocab`` -> ``.strip().lower()``), so
``POST {"tree_type": "CRT"}`` stores ``crt``; the QUERY parameter was raw
``str | None`` and was handed to the repository filter verbatim, so
``GET /toc?tree_type=CRT`` matched nothing and answered **200 with an empty
list**. An empty list is not distinguishable from "you have no CRTs", which is
why this went unseen. ``test_uppercase_filter_returns_the_same_tree`` is the
regression guard, and ``test_empty_tree_type_lists_every_tree`` is its
counterweight: a UI that unconditionally appends ``?tree_type=`` must keep
getting a full listing rather than a new 422.

⛔ A skip here is a defect, not a convenience: ``tests/integration/`` bans
``pytest.skip`` outright and rewrites any skip into a failure. Assert on the
status and body instead.
"""

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

TOC = "/toc"


def _uid():
    return uuid4().hex[:8]


@pytest.fixture
def toc_seed(req_client):
    """One ``crt`` tree with two nodes and one ``io_map`` tree with one node.

    Yields a dict of the created bodies and deletes both trees afterwards
    (node + edge rows cascade with the tree).
    """
    trees = {}
    for tree_type in ("crt", "io_map"):
        r = req_client.post(
            TOC,
            json={
                "title": f"itest-{tree_type}-{_uid()}",
                "description": "seeded by test_toc_routes",
                "tree_type": tree_type,
            },
        )
        assert r.status_code in (200, 201), f"seed {tree_type} tree -> {r.status_code}: {r.text}"
        trees[tree_type] = r.json()

    nodes = {"crt": [], "io_map": []}
    for tree_type, kinds in (("crt", ("ude", "root_cause")), ("io_map", ("goal",))):
        for kind in kinds:
            r = req_client.post(
                f"{TOC}/{trees[tree_type]['id']}/nodes",
                json={
                    "title": f"itest-{kind}-{_uid()}",
                    "statement": f"Seeded {kind} statement",
                    "node_kind": kind,
                },
            )
            assert r.status_code in (200, 201), f"seed {kind} node -> {r.status_code}: {r.text}"
            nodes[tree_type].append(r.json())

    yield {"trees": trees, "nodes": nodes}

    for tree in trees.values():
        req_client.delete(f"{TOC}/{tree['id']}")


def _titles(body):
    return [t["title"] for t in body]


def _ids(body):
    return [t["id"] for t in body]


class TestListTocTrees:
    """``GET /toc`` — the ``tree_type`` filter, in both spellings and empty."""

    def test_lowercase_filter_selects_only_that_type(self, req_client, toc_seed) -> None:
        """The control: a canonical value must still filter, and filter correctly.

        Without this, a change that made every value 422 — or that dropped the
        filter entirely — could still pass the rest of this class.
        """
        crt = toc_seed["trees"]["crt"]
        io_map = toc_seed["trees"]["io_map"]

        r = req_client.get(TOC, params={"tree_type": "crt"})
        assert r.status_code == 200, r.text
        body = r.json()

        assert crt["id"] in _ids(body), f"{crt['id']} absent from {_ids(body)}"
        assert io_map["id"] not in _ids(body), f"io_map tree leaked into a crt filter: {_ids(body)}"
        assert all(t["tree_type"] == "crt" for t in body), [t["tree_type"] for t in body]
        assert r.headers["X-Total-Count"] == str(len(body)), (
            f"X-Total-Count={r.headers.get('X-Total-Count')} but {len(body)} rows returned"
        )

    def test_uppercase_filter_returns_the_same_tree(self, req_client, toc_seed) -> None:
        """``?tree_type=CRT`` must resolve to the same rows as ``?tree_type=crt``.

        This is the defect: the body normalises, the query did not, so the
        uppercase spelling silently returned ``[]``.
        """
        crt = toc_seed["trees"]["crt"]

        lower = req_client.get(TOC, params={"tree_type": "crt"})
        upper = req_client.get(TOC, params={"tree_type": "CRT"})
        assert upper.status_code == 200, upper.text

        assert crt["id"] in _ids(upper.json()), (
            f"?tree_type=CRT returned {_ids(upper.json())}, missing the crt tree {crt['id']}"
        )
        assert sorted(_ids(upper.json())) == sorted(_ids(lower.json()))
        assert upper.headers["X-Total-Count"] == lower.headers["X-Total-Count"]

    def test_whitespace_padded_filter_returns_the_same_tree(self, req_client, toc_seed) -> None:
        """``.strip()`` on the query side, matching what the body has always done."""
        crt = toc_seed["trees"]["crt"]
        r = req_client.get(TOC, params={"tree_type": "  crt  "})
        assert r.status_code == 200, r.text
        assert crt["id"] in _ids(r.json()), _ids(r.json())

    def test_unknown_tree_type_is_rejected_not_silently_empty(self, req_client, toc_seed) -> None:
        """A typo must be a request-validation error, not an empty list.

        422 specifically — a 500 here would mean the Literal is being enforced
        somewhere below the request boundary.
        """
        r = req_client.get(TOC, params={"tree_type": "bogus"})
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
        assert "bogus" in r.text or "tree_type" in r.text, r.text

    def test_empty_tree_type_lists_every_tree(self, req_client, toc_seed) -> None:
        """``?tree_type=`` means "no filter" — a UI appending it must not 422.

        Both seeded trees must come back, so a fix that mapped the empty string
        to some sentinel value (and filtered on it) would fail here too.
        """
        crt = toc_seed["trees"]["crt"]
        io_map = toc_seed["trees"]["io_map"]

        r = req_client.get(TOC, params={"tree_type": ""})
        assert r.status_code == 200, f"empty tree_type must not 422: {r.status_code}: {r.text}"
        ids = _ids(r.json())
        assert crt["id"] in ids, ids
        assert io_map["id"] in ids, ids

        unfiltered = req_client.get(TOC)
        assert unfiltered.status_code == 200, unfiltered.text
        assert sorted(ids) == sorted(_ids(unfiltered.json()))


class TestUpdateTocTree:
    """``PUT /toc/{tree_id}`` — the handler commits twice; prove nothing is lost."""

    def test_metadata_change_reads_back_and_the_nodes_survive(self, req_client, toc_seed) -> None:
        crt = toc_seed["trees"]["crt"]
        seeded_node_ids = sorted(n["id"] for n in toc_seed["nodes"]["crt"])
        assert len(seeded_node_ids) == 2, seeded_node_ids

        before = req_client.get(f"{TOC}/{crt['id']}/nodes")
        assert before.status_code == 200, before.text
        assert sorted(_ids(before.json())) == seeded_node_ids

        new_title = f"itest-renamed-{_uid()}"
        r = req_client.put(
            f"{TOC}/{crt['id']}",
            json={
                "title": new_title,
                "description": "rewritten by the update test",
                "tree_type": "crt",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["title"] == new_title
        assert r.json()["description"] == "rewritten by the update test"

        fetched = req_client.get(f"{TOC}/{crt['id']}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["title"] == new_title, fetched.json()
        assert fetched.json()["description"] == "rewritten by the update test"
        assert fetched.json()["title"] != crt["title"]

        after = req_client.get(f"{TOC}/{crt['id']}/nodes")
        assert after.status_code == 200, after.text
        assert sorted(_ids(after.json())) == seeded_node_ids, (
            f"tree update lost children: {sorted(_ids(after.json()))} != {seeded_node_ids}"
        )

    def test_the_renamed_tree_is_still_found_by_its_type_filter(self, req_client, toc_seed) -> None:
        """An update must not move the tree out of its own listing."""
        crt = toc_seed["trees"]["crt"]
        r = req_client.put(
            f"{TOC}/{crt['id']}",
            json={"title": f"itest-still-crt-{_uid()}", "tree_type": "crt"},
        )
        assert r.status_code == 200, r.text
        listed = req_client.get(TOC, params={"tree_type": "CRT"})
        assert listed.status_code == 200, listed.text
        assert crt["id"] in _ids(listed.json()), _ids(listed.json())


class TestUpdateTocNode:
    """``PUT /toc/{tree_id}/nodes/{node_id}`` — valid, wrong-kind, wrong-tree."""

    def test_a_valid_update_reads_back(self, req_client, toc_seed) -> None:
        crt = toc_seed["trees"]["crt"]
        node = toc_seed["nodes"]["crt"][0]
        assert node["node_kind"] == "ude", node

        new_title = f"itest-node-renamed-{_uid()}"
        r = req_client.put(
            f"{TOC}/{crt['id']}/nodes/{node['id']}",
            json={
                "title": new_title,
                "statement": "The updated UDE statement",
                "node_kind": "root_cause",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["title"] == new_title
        assert r.json()["node_kind"] == "root_cause"

        listed = req_client.get(f"{TOC}/{crt['id']}/nodes")
        assert listed.status_code == 200, listed.text
        found = next((n for n in listed.json() if n["id"] == node["id"]), None)
        assert found is not None, _ids(listed.json())
        assert found["title"] == new_title
        assert found["statement"] == "The updated UDE statement"
        assert found["node_kind"] == "root_cause"
        assert found["tree_id"] == crt["id"]

    def test_a_kind_illegal_for_this_tree_type_is_422(self, req_client, toc_seed) -> None:
        """``goal`` is an io_map kind; a crt tree admits ude/intermediate_cause/root_cause."""
        crt = toc_seed["trees"]["crt"]
        node = toc_seed["nodes"]["crt"][0]

        r = req_client.put(
            f"{TOC}/{crt['id']}/nodes/{node['id']}",
            json={
                "title": f"itest-illegal-{_uid()}",
                "statement": "A goal does not belong in a current-reality tree",
                "node_kind": "goal",
            },
        )
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
        assert "goal" in r.text, r.text

        unchanged = req_client.get(f"{TOC}/{crt['id']}/nodes")
        assert unchanged.status_code == 200, unchanged.text
        found = next((n for n in unchanged.json() if n["id"] == node["id"]), None)
        assert found is not None
        assert found["node_kind"] == "ude", "the rejected update must not have been applied"

    def test_a_node_of_a_different_tree_is_404(self, req_client, toc_seed) -> None:
        """The node exists and the tree exists — they just are not related."""
        crt = toc_seed["trees"]["crt"]
        foreign = toc_seed["nodes"]["io_map"][0]

        r = req_client.put(
            f"{TOC}/{crt['id']}/nodes/{foreign['id']}",
            json={
                "title": f"itest-reparent-{_uid()}",
                "statement": "Attempting to reparent across trees",
                "node_kind": "ude",
            },
        )
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"

        io_nodes = req_client.get(f"{TOC}/{toc_seed['trees']['io_map']['id']}/nodes")
        assert io_nodes.status_code == 200, io_nodes.text
        still = next((n for n in io_nodes.json() if n["id"] == foreign["id"]), None)
        assert still is not None, "the foreign node must still belong to its own tree"
        assert still["title"] == foreign["title"]
        assert still["node_kind"] == "goal"

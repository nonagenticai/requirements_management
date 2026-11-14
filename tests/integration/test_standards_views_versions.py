"""Integration coverage for the architecture standards/views VERSION routes.

Four of the routes no test in this repo had ever called:

* ``GET /architecture/standards/{sid}/versions``
* ``GET /architecture/standards/{sid}/versions/{v1}/compare/{v2}``
* ``GET /architecture/views/{vid}/versions``
* ``GET /architecture/views/{vid}/versions/{v1}/compare/{v2}``

All four funnel into ``services.version_service.VersionService``, whose
``list_versions`` read the whole of ``entity_version_history`` for an entity
with no ``LIMIT``. It is now capped by ``MAX_VERSION_HISTORY_ROWS``; the last
test in this file is the bound's proof and goes red if the ``.limit()`` is
removed.

``architecture_standards`` and ``architecture_views`` hold ZERO rows in
production, so every test here creates its own subject and deletes it again.

⛔ A skip here is a defect, not a convenience: ``tests/integration/`` bans
``pytest.skip`` outright and rewrites any skip into a failure. Assert on the
status and body instead.
"""

import importlib
from uuid import uuid4

import pytest

# ⚠️ NOT ``from services import version_service`` — ``services/__init__.py``
# binds that name to a ``VersionService()`` INSTANCE, which shadows the
# submodule. The cap lives on the module, so fetch the module itself.
version_service_module = importlib.import_module("services.version_service")

pytestmark = pytest.mark.integration

STANDARDS = "/architecture/standards"
VIEWS = "/architecture/views"


def _uid():
    return uuid4().hex[:8]


def _versions_of(payload) -> list[str]:
    return [entry["version"] for entry in payload]


@pytest.fixture
def standard_with_history(req_client):
    """A standard carrying three snapshots: 1.0.0 (create), 1.1.0, 1.2.0.

    Yields ``(sid, [descriptions keyed by version])`` and deletes the standard
    afterwards.
    """
    title = f"itest-std-{_uid()}"
    r = req_client.post(
        STANDARDS,
        json={
            "title": title,
            "description": "desc-at-1.0.0",
            "standard_type": "technology_standard",
            "version": "1.0.0",
        },
    )
    assert r.status_code in (200, 201), r.text
    sid = r.json()["id"]

    descriptions = {"1.0.0": "desc-at-1.0.0"}
    for version in ("1.1.0", "1.2.0"):
        description = f"desc-at-{version}"
        u = req_client.put(
            f"{STANDARDS}/{sid}",
            json={
                "title": title,
                "description": description,
                "standard_type": "technology_standard",
                "version": version,
            },
        )
        assert u.status_code == 200, u.text
        assert u.json()["version"] == version, u.text
        descriptions[version] = description

    yield sid, descriptions

    req_client.delete(f"{STANDARDS}/{sid}")


@pytest.fixture
def view_with_history(req_client):
    """A view carrying three snapshots: 1.0.0 (create), 1.1.0, 1.2.0."""
    title = f"itest-view-{_uid()}"
    r = req_client.post(
        VIEWS,
        json={
            "title": title,
            "description": "desc-at-1.0.0",
            "viewpoint": "layered",
            "version": "1.0.0",
        },
    )
    assert r.status_code in (200, 201), r.text
    vid = r.json()["id"]

    descriptions = {"1.0.0": "desc-at-1.0.0"}
    for version in ("1.1.0", "1.2.0"):
        description = f"desc-at-{version}"
        u = req_client.put(
            f"{VIEWS}/{vid}",
            json={
                "title": title,
                "description": description,
                "viewpoint": "layered",
                "version": version,
            },
        )
        assert u.status_code == 200, u.text
        assert u.json()["version"] == version, u.text
        descriptions[version] = description

    yield vid, descriptions

    req_client.delete(f"{VIEWS}/{vid}")


class TestStandardVersions:
    def test_lists_the_versions_that_were_created_newest_first(
        self, req_client, standard_with_history
    ):
        sid, _ = standard_with_history
        r = req_client.get(f"{STANDARDS}/{sid}/versions")
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, list), body
        versions = _versions_of(body)
        # The history is real, not the single synthesised entry the service
        # falls back to when a table read returns nothing.
        assert len(versions) >= 2, versions
        # The versions are the ones this fixture created, not arbitrary rows.
        assert {"1.0.0", "1.1.0", "1.2.0"} <= set(versions), versions
        # Newest first: descending, as the route's ORDER BY promises.
        assert versions == sorted(versions, reverse=True), versions
        assert versions[0] == "1.2.0", versions

    def test_compare_names_the_field_that_actually_changed(self, req_client, standard_with_history):
        sid, descriptions = standard_with_history
        r = req_client.get(f"{STANDARDS}/{sid}/versions/1.1.0/compare/1.2.0")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["old_version"] == "1.1.0", body
        assert body["new_version"] == "1.2.0", body
        diff = body["version_diff"]
        # description is the ONLY field the fixture changed between these two
        # snapshots, so it must be named and title must not be.
        assert "description" in diff, diff
        assert descriptions["1.1.0"] in diff["description"], diff
        assert descriptions["1.2.0"] in diff["description"], diff
        assert "title" not in diff, diff

    def test_compare_against_an_unknown_version_is_4xx_never_500(
        self, req_client, standard_with_history
    ):
        sid, _ = standard_with_history
        r = req_client.get(f"{STANDARDS}/{sid}/versions/1.1.0/compare/9.9.9")
        assert r.status_code < 500, (r.status_code, r.text)
        assert 400 <= r.status_code < 500, (r.status_code, r.text)

    def test_compare_with_an_unparseable_version_is_4xx_never_500(
        self, req_client, standard_with_history
    ):
        sid, _ = standard_with_history
        r = req_client.get(f"{STANDARDS}/{sid}/versions/1.1.0/compare/not-a-version")
        assert r.status_code < 500, (r.status_code, r.text)
        assert 400 <= r.status_code < 500, (r.status_code, r.text)


class TestViewVersions:
    def test_lists_the_versions_that_were_created_newest_first(self, req_client, view_with_history):
        vid, _ = view_with_history
        r = req_client.get(f"{VIEWS}/{vid}/versions")
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, list), body
        versions = _versions_of(body)
        assert len(versions) >= 2, versions
        assert {"1.0.0", "1.1.0", "1.2.0"} <= set(versions), versions
        assert versions == sorted(versions, reverse=True), versions
        assert versions[0] == "1.2.0", versions

    def test_compare_names_the_field_that_actually_changed(self, req_client, view_with_history):
        vid, descriptions = view_with_history
        r = req_client.get(f"{VIEWS}/{vid}/versions/1.1.0/compare/1.2.0")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["old_version"] == "1.1.0", body
        assert body["new_version"] == "1.2.0", body
        diff = body["version_diff"]
        assert "description" in diff, diff
        assert descriptions["1.1.0"] in diff["description"], diff
        assert descriptions["1.2.0"] in diff["description"], diff
        assert "title" not in diff, diff

    def test_compare_against_an_unknown_version_is_4xx_never_500(
        self, req_client, view_with_history
    ):
        vid, _ = view_with_history
        r = req_client.get(f"{VIEWS}/{vid}/versions/1.1.0/compare/9.9.9")
        assert r.status_code < 500, (r.status_code, r.text)
        assert 400 <= r.status_code < 500, (r.status_code, r.text)


class TestVersionHistoryIsBounded:
    """The bound's proof. Delete the ``.limit()`` in ``version_service`` and
    this goes red — the fixture writes 3 snapshots and the cap is set to 2."""

    def test_history_is_capped_at_max_version_history_rows(
        self, req_client, standard_with_history, monkeypatch
    ):
        sid, _ = standard_with_history

        # Unbounded first: prove the fixture really produced more rows than the
        # cap we are about to set, so a green result below cannot be vacuous.
        unbounded = req_client.get(f"{STANDARDS}/{sid}/versions")
        assert unbounded.status_code == 200, unbounded.text
        assert len(unbounded.json()) >= 3, unbounded.text

        monkeypatch.setattr(version_service_module, "MAX_VERSION_HISTORY_ROWS", 2)
        capped = req_client.get(f"{STANDARDS}/{sid}/versions")
        assert capped.status_code == 200, capped.text
        rows = capped.json()
        assert len(rows) == 2, rows
        # Still the newest ones, and still ordered.
        assert _versions_of(rows) == ["1.2.0", "1.1.0"], rows

    def test_the_bound_is_in_the_SQL_and_not_only_in_python(
        self, req_client, standard_with_history, monkeypatch
    ):
        """⛔ THE ROW-COUNT TEST ABOVE CANNOT SEE THIS, AND THAT IS WHY THIS EXISTS.

        ``list_versions`` reads ``cap + 1`` rows as a truncation probe and then
        slices the extra one off in Python. That slice means the row count the
        caller observes is ``cap`` whether or not the SQL carried a ``LIMIT`` —
        so deleting ``.limit(...)`` leaves the test above GREEN while the
        database happily streams every row of the history to the process. That is
        not a bound at all, it is a bound-shaped assertion.

        Measured: with ``.limit()`` deleted, the count test passed 8/8. This test
        goes red on that same mutation because it asserts on the statement the
        database was actually asked to run.
        """
        import sqlalchemy.event
        from sqlalchemy.engine import Engine

        sid, _ = standard_with_history
        monkeypatch.setattr(version_service_module, "MAX_VERSION_HISTORY_ROWS", 2)

        seen: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            if "entity_version_history" in statement:
                seen.append(" ".join(statement.split()))

        sqlalchemy.event.listen(Engine, "before_cursor_execute", _record)
        try:
            resp = req_client.get(f"{STANDARDS}/{sid}/versions")
        finally:
            sqlalchemy.event.remove(sqlalchemy.engine.Engine, "before_cursor_execute", _record)

        assert resp.status_code == 200, resp.text
        # Control: the listener must actually have caught the read, otherwise an
        # empty `seen` would make every assertion below vacuously true.
        selects = [q for q in seen if q.lower().startswith("select")]
        assert selects, f"no entity_version_history SELECT was captured: {seen}"

        limited = [q for q in selects if "limit" in q.lower()]
        assert limited, (
            "the version-history SELECT carried NO LIMIT clause -- the cap is "
            f"being applied in Python only, so the database returned the whole "
            f"history. Statements seen: {selects}"
        )

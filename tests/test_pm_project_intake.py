"""The fr project intake on ``pm_projects`` + versioned briefs (George 2026-10-13).

George's flow: the user creates a project in fr → fr stores it in RM ``pm_projects`` → fr
calls the conveyor webhook with ONLY the RM project id → n8n reads everything the user
provided from RM in one ``GET /pm/projects/{eid}``. RM stores the brief; the conveyor's
enhancements are stored as further versions; fr shows the brief with versions.

All tests hit real PostgreSQL through the FastAPI test client.
"""

import asyncio
import copy
import uuid

import pytest
from httpx import AsyncClient, MockTransport, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.briefs import MIN_BRIEF
from tests.project_types import typed
from tests.tenants import T
from tests.test_pm_catalogue import _As, _post

SECRET = "0199a1b2-0000-7000-8000-00000000a001"
DOC = "0199a1b2-0000-7000-8000-00000000d001"
APPROVER = "0199a1b2-0000-7000-8000-0000000000a1"
REPO_ROW = "0199a1b2-0000-7000-8000-0000000000f1"

# A full create body as fr builds it, keyed by fr's own zod names. Source:
# fr ``backend/src/types/api.ts`` ``ProjectCreateFields`` on feat/fr-rm-pm-catalogue
# (de6c7763); the line of each field is cited. fr maps ``name`` → ``title`` and
# ``requestor`` → ``requested_by``; ``repo`` / ``repository_url`` / ``branch`` →
# ``repositories``; the brief fields are grouped under ``brief``; the IT fields under
# ``it_inputs``; ``secret_ids`` / ``document_ids`` under ``attachments``.
FR_BRIEF = {
    "problem_statement": "Checkout times out under load",  # :425
    "desired_outcome": "p95 checkout < 2s",  # :426
    "business_case": "Abandoned carts cost 4% revenue",  # :427
    "success_criteria": ["p95 < 2s for 7 days"],  # :430
    "acceptance_criteria": ["load test at 500 rps passes"],  # :431
    "in_scope": ["checkout service"],  # :432
    "out_of_scope": [],  # :433 — an empty list is kept as given
    "high_level_requirements": ["cache the price lookup"],  # :434
    "assumptions": ["traffic doubles in Q4"],  # :435
    "access_requirements": ["read access to prod metrics"],  # :436
    "stakeholders": ["payments team"],  # :421
    "approvers": [APPROVER],  # :422
    "deadline": "2026-12-01",  # :473 — kept as typed, not mapped onto end_date
    "budget": "20k EUR",  # :486 — kept as typed, not mapped onto budget
    "notes": "talk to Ana first",  # :487
    "constraints": [  # :481 + types/project-constraints.ts
        "do not touch the payments SDK",
        {"kind": "no_language_version_change"},
        {"kind": "pin_language_version", "language": "python", "version": "3.14"},
        {"kind": "no_dependency_upgrades"},
        {"kind": "no_new_dependencies"},
        {"kind": "no_files_touched", "globs": ["migrations/**"]},
        {"kind": "must_keep_api_compatible"},
        {"kind": "custom", "text": "keep the feature flag"},
    ],
    "dependencies": [  # :482
        "redis",
        {"name": "pricing-api", "kind": "api", "note": "v2 only"},
        {"name": "orders", "kind": "service"},
    ],
    "risks": [  # :483 (RiskSchema :216)
        {"description": "cache staleness", "likelihood": "medium", "impact": "high"},
        {"description": "load test env drift", "mitigation": "pin the env"},
    ],
}
FR_IT_INPUTS = {
    "it_flow": "bug",  # :366
    "min_severity": "HIGH",  # :399
    "cve_sweep": False,  # :510
    "bug_hunt": False,  # :518
    "target_packages": [{"name": "fastapi", "target_version": "0.130.0"}],  # :497
    "test_command": ["/workspace/.venv/bin/python", "-m", "pytest", "-q"],  # :400
    "test_workdir": "/workspace",  # :401
    "bug_test_command": ["pytest", "tests/test_checkout.py::test_timeout"],  # :402
    "failing_test_ref": "tests/test_checkout.py::test_timeout",  # :403
    "target_files": ["src/checkout.py"],  # :404
    "finding_description": "n/a",  # :405
    "bug_description": "checkout hangs when the price cache is cold",  # :406
}
FR_PAYLOAD = {
    "title": "Checkout latency",  # :283 name
    "description": "Make checkout fast",  # :284
    "repositories": [  # :447 repo.primary / repo.additional[]
        {"url": "https://github.com/acme/shop", "branch": "main", "path": "svc", "primary": True},
        {"url": "https://github.com/acme/pricing", "branch": "dev", "name": "pricing"},
    ],
    "repository_id": REPO_ROW,  # :470
    "coordination_mode": "independent",  # :337
    "scope_level": "multi_repo",  # :305
    "shape": "deliverable",  # :492
    "priority": "P1",  # :312 (CARD_PRIORITIES)
    "expected_size": "M",  # v3
    "requested_by": "ana@acme.test",  # :409 requestor
    "github_token_secret_id": SECRET,  # the secret id fr got back for :291
    "question_mode": "structured",  # :378
    "lifecycle_class": "execution",  # :317
    "fixture": True,  # :420
    "it_inputs": FR_IT_INPUTS,
    "attachments": {"secret_ids": [SECRET], "document_ids": [DOC]},  # :468-469
    "bound_assets": {"procedure": ["proc-a"], "requirement": [APPROVER]},  # :526
    "brief": FR_BRIEF,
}

INTAKE_OUT_FIELDS = (
    "repositories",
    "repository_id",
    "coordination_mode",
    "scope_level",
    "shape",
    "priority_level_id",
    "priority",
    "expected_size",
    "requested_by",
    "github_token_secret_id",
    "question_mode",
    "lifecycle_class",
    "fixture",
    "it_inputs",
    "attachments",
    "bound_assets",
    "brief",
    "brief_version",
    "brief_author_kind",
    "brief_updated_at",
    "intake_updated_at",
)


def _strip_none(v):
    if isinstance(v, dict):
        return {k: _strip_none(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_strip_none(x) for x in v]
    return v


async def _levels(c) -> dict[str, dict]:
    await _post(c, "/pm/catalogue/import-defaults", {})
    return {r["name"]: r for r in (await c.get("/pm/priority-levels")).json()}


async def _project(c, **kw) -> dict:
    """A project; the brief is required on create, so the minimal one unless given."""
    return await _post(
        c, "/pm/projects", await typed(c, {"title": "p", "brief": copy.deepcopy(MIN_BRIEF), **kw})
    )


async def _put(c, pid: str, **kw):
    return await c.put(f"/pm/projects/{pid}", json={"title": "p", **kw})


async def _append(c, pid: str, brief: dict, **kw):
    return await c.post(f"/pm/projects/{pid}/brief/versions", json={"brief": brief, **kw})


def _code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or {}).get("code") if isinstance(body, dict) else None


async def _make_legacy(session: AsyncSession, pid: str) -> None:
    """Turn a project into a pre-0056 legacy row: no brief versions, no current version."""
    await session.execute(
        text("DELETE FROM pm_project_brief_versions WHERE project_id = :id"), {"id": pid}
    )
    await session.execute(
        text(
            "UPDATE pm_projects SET repositories = NULL, it_inputs = NULL, "
            "intake_updated_at = NULL, current_brief_version = NULL WHERE id = :id"
        ),
        {"id": pid},
    )
    await session.commit()


def _msg(resp) -> str:
    return resp.text


# ---------------------------------------------------------------------------
# The intake round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    async def test_full_fr_payload_round_trips(self, authenticated_client: AsyncClient):
        c = authenticated_client
        levels = await _levels(c)
        created = await _project(c, **copy.deepcopy(FR_PAYLOAD))
        got = (await c.get(f"/pm/projects/{created['id']}")).json()
        assert got == created
        for k in ("coordination_mode", "scope_level", "shape", "expected_size", "requested_by"):
            assert got[k] == FR_PAYLOAD[k], k
        for k in ("question_mode", "lifecycle_class", "fixture"):
            assert got[k] == FR_PAYLOAD[k], k
        assert got["repository_id"] == REPO_ROW
        assert got["github_token_secret_id"] == SECRET
        assert got["priority"] == "p1"
        assert got["priority_level_id"] == levels["p1"]["id"]
        assert _strip_none(got["repositories"]) == [
            FR_PAYLOAD["repositories"][0],
            {**FR_PAYLOAD["repositories"][1], "primary": False},
        ]
        assert _strip_none(got["it_inputs"]) == FR_IT_INPUTS
        assert got["attachments"] == FR_PAYLOAD["attachments"]
        assert _strip_none(got["bound_assets"]) == FR_PAYLOAD["bound_assets"]
        # Acceptance criteria are stored as items (fr cockpit A4): the strings fr sends come
        # back as ``unchecked`` items with server-minted ids.
        brief = _strip_none(got["brief"])
        items = brief.pop("acceptance_criteria")
        assert [(i["text"], i["status"], i["evidence"]) for i in items] == [
            (t, "unchecked", []) for t in FR_BRIEF["acceptance_criteria"]
        ]
        assert all(i["id"] for i in items)
        assert brief == {k: v for k, v in FR_BRIEF.items() if k != "acceptance_criteria"}
        # The test client is a trusted-network caller, not fr-delegated → conveyor.
        assert got["brief_version"] == 1 and got["brief_author_kind"] == "conveyor"
        assert got["brief_updated_at"] is not None
        assert got["intake_updated_at"] is not None
        # ``title`` / ``description`` are the existing columns; deadline/budget stayed in brief.
        assert got["title"] == "Checkout latency" and got["end_date"] is None
        assert got["budget"] is None

    async def test_stored_json_drops_null_keys(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        await _levels(authenticated_client)
        p = await _project(authenticated_client, **copy.deepcopy(FR_PAYLOAD))
        row = (
            await test_session.execute(
                text("SELECT repositories, it_inputs FROM pm_projects WHERE id = :id"),
                {"id": p["id"]},
            )
        ).one()
        assert row.repositories[1] == {
            "url": "https://github.com/acme/pricing",
            "branch": "dev",
            "name": "pricing",
            "primary": False,
        }
        assert row.it_inputs == FR_IT_INPUTS

    async def test_legacy_row_reads_and_updates(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        c = authenticated_client
        p = await _project(c)
        # A legacy row: every intake column NULL (as before migration 0056).
        await _make_legacy(test_session, p["id"])
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert all(got[k] is None for k in INTAKE_OUT_FIELDS), {
            k: got[k] for k in INTAKE_OUT_FIELDS if got[k] is not None
        }
        resp = await c.put(f"/pm/projects/{p['id']}", json={"title": "renamed"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "renamed"
        assert all(resp.json()[k] is None for k in INTAKE_OUT_FIELDS)
        listed = await c.get(f"/pm/projects/{p['id']}/brief/versions")
        assert listed.status_code == 200 and listed.json() == []
        assert listed.headers["X-Total-Count"] == "0"
        assert (await c.get(f"/pm/projects/{p['id']}/brief/versions/1")).status_code == 404
        # The legacy row takes the intake later — but never a brief: v1 is written only at
        # creation, so neither PUT nor an append can give it one (409 brief_missing_v1).
        resp = await _put(c, p["id"], scope_level="repo", brief={"notes": "late"})
        assert resp.status_code == 409 and _code(resp) == "brief_missing_v1", resp.text
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["scope_level"] is None
        resp = await _append(c, p["id"], {"notes": "late"})
        assert resp.status_code == 409 and _code(resp) == "brief_missing_v1", resp.text
        resp = await _put(c, p["id"], scope_level="repo")
        assert resp.status_code == 200, resp.text
        assert resp.json()["scope_level"] == "repo" and resp.json()["brief_version"] is None
        assert (await c.get(f"/pm/projects/{p['id']}/brief/versions")).json() == []


# ---------------------------------------------------------------------------
# PUT semantics: omitted keeps, explicit null clears
# ---------------------------------------------------------------------------

GROUPS: dict[str, tuple[object, object]] = {
    # field: (initial value, a different value)
    "repositories": ([{"url": "https://a", "primary": True}], [{"url": "https://b"}]),
    "repository_id": (REPO_ROW, SECRET),
    "coordination_mode": ("coordinated", "independent"),
    "scope_level": ("repo", "module"),
    "expected_size": ("S", "L"),
    "requested_by": ("ana", "bob"),
    "github_token_secret_id": (SECRET, REPO_ROW),
    "question_mode": ("structured", "free_text"),
    "lifecycle_class": ("system", "execution"),
    "fixture": (True, False),
    "it_inputs": ({"it_flow": "bug", "cve_sweep": True}, {"it_flow": "refactor"}),
    "attachments": ({"secret_ids": [SECRET], "document_ids": []}, {"document_ids": [DOC]}),
    "bound_assets": ({"procedure": ["a"]}, {"workflow": ["w"]}),
}


def _cmp(field: str, value):
    if field == "repositories":
        return [{"branch": None, "path": None, "name": None, "primary": False, **r} for r in value]
    if field == "attachments":
        return {"secret_ids": [], "document_ids": [], **value}
    if field == "it_inputs":
        keys = (
            "it_flow min_severity cve_sweep bug_hunt target_packages test_command test_workdir "
            "bug_test_command failing_test_ref target_files finding_description bug_description"
        ).split()
        return {k: value.get(k) for k in keys}
    if field == "bound_assets":
        kinds = (
            "requirement principle decision procedure kag_document workflow trigger artifact "
            "channel"
        ).split()
        return {k: value.get(k) for k in kinds}
    return value


class TestPutSemantics:
    @pytest.mark.parametrize("field", sorted(GROUPS))
    async def test_omitted_keeps_change_replaces_null_clears(
        self, authenticated_client: AsyncClient, field: str
    ):
        c = authenticated_client
        first, second = GROUPS[field]
        p = await _project(c, **{field: first})
        assert p[field] == _cmp(field, first)
        kept = await _put(c, p["id"], description="other")
        assert kept.status_code == 200, kept.text
        assert kept.json()[field] == _cmp(field, first), "omitted must keep"
        changed = await _put(c, p["id"], **{field: second})
        assert changed.status_code == 200, changed.text
        assert changed.json()[field] == _cmp(field, second), "a sent value replaces whole"
        cleared = await _put(c, p["id"], **{field: None})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()[field] is None
        assert (await c.get(f"/pm/projects/{p['id']}")).json()[field] is None

    async def test_priority_omitted_keeps_null_clears(self, authenticated_client: AsyncClient):
        c = authenticated_client
        levels = await _levels(c)
        p = await _project(c, priority="P0")
        assert p["priority"] == "p0"
        kept = (await _put(c, p["id"], description="x")).json()
        assert kept["priority_level_id"] == levels["p0"]["id"]
        by_id = (await _put(c, p["id"], priority_level_id=levels["p2"]["id"])).json()
        assert by_id["priority"] == "p2"
        cleared = (await _put(c, p["id"], priority=None)).json()
        assert cleared["priority"] is None and cleared["priority_level_id"] is None
        again = (await _put(c, p["id"], priority="p3")).json()
        assert again["priority"] == "p3"
        cleared = (await _put(c, p["id"], priority_level_id=None)).json()
        assert cleared["priority_level_id"] is None

    async def test_shape_is_immutable_once_set(self, authenticated_client: AsyncClient):
        c = authenticated_client
        legacy = await _project(c)
        assert (await _put(c, legacy["id"], shape="continuous_flow")).status_code == 200
        p = await _project(c, shape="deliverable")
        assert (await _put(c, p["id"], shape="deliverable")).status_code == 200
        assert (await _put(c, p["id"], description="x")).json()["shape"] == "deliverable"
        for bad in ("continuous_flow", None):
            resp = await _put(c, p["id"], shape=bad)
            assert resp.status_code == 409, resp.text
            assert "immutable" in _msg(resp)
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["shape"] == "deliverable"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {"coordination_mode": "together"},
            {"scope_level": "single_repo"},  # fr's legacy alias is refused at fr's door too
            {"scope_level": "Repo"},
            {"shape": "project"},
            {"question_mode": "Structured"},
            {"lifecycle_class": "business"},
            {"expected_size": "XL"},
            {"expected_size": "m"},
            {"it_inputs": {"it_flow": "perf"}},
            {"it_inputs": {"min_severity": "low"}},
            {"it_inputs": {"unknown": 1}},
            {"it_inputs": {"approval_mode": "never"}},  # not one of the 4 modes
            {"it_inputs": {"test_command": "pytest -q"}},  # argv, not a string
            {"attachments": {"secret_ids": ["not-a-uuid"]}},
            {"attachments": {"tokens": []}},
            {"bound_assets": {"unicorn": ["x"]}},
            {"bound_assets": {"procedure": [""]}},
            {"repositories": [{"branch": "main"}]},  # url required
            {"repositories": [{"url": ""}]},
            {"repositories": [{"url": "a", "primary": True}, {"url": "b", "primary": True}]},
            {"repositories": [{"url": "a", "token": "x"}]},
            {"brief": {"problem_statement": "x", "unknown": 1}},
            {"brief": {"constraints": [{"kind": "no_rm_rf"}]}},
            {"brief": {"constraints": [{"kind": "pin_language_version", "language": "py"}]}},
            {"brief": {"constraints": [{"kind": "no_files_touched", "globs": []}]}},
            {"brief": {"dependencies": [{"name": "x", "kind": "database"}]}},
            {"brief": {"risks": [{"likelihood": "high"}]}},  # description required
            {"brief": {"approvers": [""]}},  # each approver is a non-empty string
            {"brief": {"approvers": ["a" * 256]}},  # ≤ 255 characters
            {"brief": None},
            {"requested_by": "x" * 256},
        ],
    )
    async def test_invalid_is_422_and_writes_nothing(
        self, authenticated_client: AsyncClient, body: dict
    ):
        c = authenticated_client
        resp = await c.post(
            "/pm/projects", json=await typed(c, {"title": "bad", "brief": MIN_BRIEF, **body})
        )
        assert resp.status_code == 422, resp.text
        p = await _project(c, shape="deliverable", scope_level="repo")
        put = await _put(c, p["id"], **body)
        assert put.status_code == 422, put.text
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert got["scope_level"] == "repo" and got["brief_version"] == 1
        assert [x["title"] for x in (await c.get("/pm/projects")).json()] == ["p"]

    async def test_every_fr_vocabulary_value_is_accepted(self, authenticated_client: AsyncClient):
        c = authenticated_client
        vocab = {
            "scope_level": [
                "multi_repo",
                "repo",
                "app",
                "module",
                "component",
                "file",
                "class",
                "function",
            ],  # fmt: skip
            "coordination_mode": ["coordinated", "independent"],
            "question_mode": ["structured", "free_text"],
            "lifecycle_class": [
                "system",
                "workflow-improvement",
                "execution",
                "prompt-improvement",
            ],  # fmt: skip
            "expected_size": ["S", "M", "L"],
        }
        for field, values in vocab.items():
            for v in values:
                assert (await _project(c, **{field: v}))[field] == v
        for shape in ("deliverable", "continuous_flow"):
            assert (await _project(c, shape=shape))["shape"] == shape
        for flow in ("dependency", "security", "source-security", "iac", "bug", "refactor"):
            assert (await _project(c, it_inputs={"it_flow": flow}))["it_inputs"]["it_flow"] == flow
        for sev in ("LOW", "MODERATE", "HIGH", "CRITICAL"):
            got = await _project(c, it_inputs={"min_severity": sev})
            assert got["it_inputs"]["min_severity"] == sev

    async def test_raw_github_token_is_refused(self, authenticated_client: AsyncClient):
        c = authenticated_client
        token = "ghp_" + "a" * 36  # pragma: allowlist secret
        resp = await c.post(
            "/pm/projects", json=await typed(c, {"title": "t", "github_access_token": token})
        )
        assert resp.status_code == 422, resp.text
        assert "github_access_token_refused_send_github_token_secret_id" in resp.text
        assert token not in resp.text, "the refusal must not echo the token"
        assert (await c.get("/pm/projects")).json() == []
        p = await _project(c, github_token_secret_id=SECRET)
        put = await _put(c, p["id"], github_access_token=token)
        assert put.status_code == 422, put.text
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["github_token_secret_id"] == SECRET

    async def test_database_checks(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        p = await _project(authenticated_client)
        for stmt, name in (
            ("UPDATE pm_projects SET expected_size = 'X' WHERE id = :id", "expected_size"),
            ("UPDATE pm_projects SET current_brief_version = 0 WHERE id = :id", "brief_version"),
        ):
            with pytest.raises(IntegrityError, match=name):
                await test_session.execute(text(stmt), {"id": p["id"]})
            await test_session.rollback()
        await test_session.execute(
            text("UPDATE pm_projects SET expected_size = 'L' WHERE id = :id"), {"id": p["id"]}
        )
        await test_session.commit()


class TestSizeLimits:
    async def test_string_limit(self, authenticated_client: AsyncClient):
        c = authenticated_client
        ok = await c.post(
            "/pm/projects", json=await typed(c, {"title": "ok", "brief": {"notes": "x" * 10_000}})
        )
        assert ok.status_code == 200, ok.text
        for body in (
            {"brief": {"notes": "x" * 10_001}},
            {"brief": {"in_scope": ["x" * 10_001]}},
            {"it_inputs": {"bug_description": "x" * 10_001}},
            {"repositories": [{"url": "x" * 10_001}]},
        ):
            resp = await c.post("/pm/projects", json=await typed(c, {"title": "big", **body}))
            assert resp.status_code == 422, (body.keys(), resp.text[:200])

    async def test_list_limit(self, authenticated_client: AsyncClient):
        c = authenticated_client
        ok = await c.post(
            "/pm/projects", json=await typed(c, {"title": "ok", "brief": {"in_scope": ["x"] * 200}})
        )
        assert ok.status_code == 200, ok.text
        for body in (
            {"brief": {"in_scope": ["x"] * 201}},
            {"brief": {"risks": [{"description": "r"}] * 201}},
            {"repositories": [{"url": "u"}] * 201},
            {"attachments": {"document_ids": [DOC] * 201}},
            {"bound_assets": {"procedure": ["p"] * 201}},
            {"it_inputs": {"target_files": ["f"] * 201}},
        ):
            resp = await c.post("/pm/projects", json=await typed(c, {"title": "big", **body}))
            assert resp.status_code == 422, (body.keys(), resp.text[:200])

    async def test_brief_and_it_inputs_byte_limits(self, authenticated_client: AsyncClient):
        c = authenticated_client
        # Each string and each list is within its own limit; the whole is not.
        big_brief = {"in_scope": ["x" * 1000] * 140}  # ~140 KB > 128 KiB
        resp = await c.post(
            "/pm/projects", json=await typed(c, {"title": "big", "brief": big_brief})
        )
        assert resp.status_code == 422, resp.text[:300]
        assert "brief_over_128_kib" in resp.text
        under = {"in_scope": ["x" * 1000] * 120}  # ~120 KB
        assert (
            await c.post("/pm/projects", json=await typed(c, {"title": "ok", "brief": under}))
        ).status_code == 200
        big_it = {"target_files": ["f" * 400] * 170}  # ~68 KB > 64 KiB
        resp = await c.post(
            "/pm/projects", json=await typed(c, {"title": "big", "it_inputs": big_it})
        )
        assert resp.status_code == 422, resp.text[:300]
        assert "it_inputs_over_64_kib" in resp.text
        p = await _project(c)
        assert (await _append(c, p["id"], big_brief)).status_code == 422


# ---------------------------------------------------------------------------
# Priority against the tenant's catalogue
# ---------------------------------------------------------------------------


class TestPriority:
    async def test_name_is_case_insensitive_and_must_exist(self, authenticated_client: AsyncClient):
        c = authenticated_client
        none = await c.post(
            "/pm/projects",
            json=await typed(c, {"title": "x", "brief": MIN_BRIEF, "priority": "P1"}),
        )
        assert none.status_code == 422, "a tenant with no levels has no P1"
        levels = await _levels(c)
        for spelled in ("P1", "p1", " p1 "):
            assert (await _project(c, priority=spelled))["priority_level_id"] == levels["p1"]["id"]
        bad = await c.post(
            "/pm/projects",
            json=await typed(c, {"title": "x", "brief": MIN_BRIEF, "priority": "P9"}),
        )
        assert bad.status_code == 422 and "p0" in bad.text, bad.text
        both = await c.post(
            "/pm/projects",
            json=await typed(
                c,
                {
                    "title": "x",
                    "brief": MIN_BRIEF,
                    "priority": "P1",
                    "priority_level_id": levels["p2"]["id"],
                },
            ),
        )
        assert both.status_code == 422, both.text
        same = await _project(c, priority="P2", priority_level_id=levels["p2"]["id"])
        assert same["priority"] == "p2"
        assert (
            await c.post(
                "/pm/projects",
                json=await typed(
                    c, {"title": "x", "brief": MIN_BRIEF, "priority_level_id": SECRET}
                ),
            )
        ).status_code == 422

    async def test_other_tenants_levels_never_count(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        a_levels = await _levels(a)
        # B has no catalogue: A's names do not resolve for B, nor do A's ids.
        assert (
            await b.post("/pm/projects", json=await typed(b, {"title": "b", "priority": "p1"}))
        ).status_code == 422
        foreign = await b.post(
            "/pm/projects",
            json=await typed(b, {"title": "b", "priority_level_id": a_levels["p1"]["id"]}),
        )
        assert foreign.status_code == 422, foreign.text
        b_levels = await _levels(b)
        pb = await _project(b, priority="p1")
        assert pb["priority_level_id"] == b_levels["p1"]["id"] != a_levels["p1"]["id"]
        put = await b.put(
            f"/pm/projects/{pb['id']}",
            json={"title": "p", "priority_level_id": a_levels["p0"]["id"]},
        )
        assert put.status_code == 422, put.text
        assert (await b.get(f"/pm/projects/{pb['id']}")).json()["priority"] == "p1"

    async def test_level_in_use_cannot_be_deleted(self, authenticated_client: AsyncClient):
        c = authenticated_client
        levels = await _levels(c)
        p = await _project(c, priority="p3")
        resp = await c.delete(f"/pm/priority-levels/{levels['p3']['id']}")
        assert resp.status_code == 409, resp.text
        assert p["slug"] in resp.text
        await _put(c, p["id"], priority=None)
        assert (await c.delete(f"/pm/priority-levels/{levels['p3']['id']}")).status_code == 200


# ---------------------------------------------------------------------------
# intake_updated_at
# ---------------------------------------------------------------------------


class TestIntakeUpdatedAt:
    async def test_changes_only_when_an_intake_field_changes(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        await _levels(c)
        # The brief is an intake field and is required on create, so every new project
        # starts with an intake timestamp.
        plain = await _project(c)
        assert plain["intake_updated_at"] is not None
        p = await _project(c, scope_level="repo", it_inputs={"it_flow": "bug"}, priority="p1")
        t0 = p["intake_updated_at"]
        assert t0 is not None
        same = await _put(
            c,
            p["id"],
            description="non-intake change",
            scope_level="repo",
            it_inputs={"it_flow": "bug"},
            priority="P1",
        )
        assert same.json()["intake_updated_at"] == t0, "re-sending the same values is no change"
        assert (await _put(c, p["id"], wip_limit=3)).json()["intake_updated_at"] == t0
        t1 = (await _put(c, p["id"], scope_level="module")).json()["intake_updated_at"]
        assert t1 > t0
        t2 = (await _put(c, p["id"], it_inputs={"it_flow": "iac"})).json()["intake_updated_at"]
        assert t2 > t1
        t3 = (await _put(c, p["id"], priority="p2")).json()["intake_updated_at"]
        assert t3 > t2
        t4 = (await _put(c, p["id"], scope_level=None)).json()["intake_updated_at"]
        assert t4 > t3
        t5 = (await _put(c, p["id"], brief={"notes": "n"})).json()["intake_updated_at"]
        assert t5 > t4
        resp = await _append(c, p["id"], {"notes": "conveyor"})
        assert resp.status_code == 200, resp.text
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["intake_updated_at"] > t5


# ---------------------------------------------------------------------------
# Brief versions
# ---------------------------------------------------------------------------


class TestBriefVersions:
    async def test_v1_on_create(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c, brief={"problem_statement": "x"})
        assert (p["brief_version"], p["brief_author_kind"]) == (1, "conveyor")
        assert p["brief"]["problem_statement"] == "x"

    async def test_brief_is_required_on_create(self, authenticated_client: AsyncClient):
        c = authenticated_client
        resp = await c.post("/pm/projects", json=await typed(c, {"title": "no brief"}))
        assert resp.status_code == 422 and _code(resp) == "brief_required", resp.text
        assert (await c.get("/pm/projects")).json() == []
        # The origin_project_id get-or-create path: a brief-less POST that would CREATE.
        origin = {"origin_kind": "project", "origin_project_id": str(uuid.uuid4())}
        resp = await c.post("/pm/projects", json=await typed(c, {"title": "minted", **origin}))
        assert resp.status_code == 422 and _code(resp) == "brief_required", resp.text
        assert (await c.get("/pm/projects")).json() == []

    async def test_origin_repost_onto_an_existing_row_needs_no_brief(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        origin = {"origin_kind": "project", "origin_project_id": str(uuid.uuid4())}
        p = await _project(c, title="minted", description="one", **origin)
        again = await c.post(
            "/pm/projects", json=await typed(c, {"title": "minted", "description": "two", **origin})
        )
        assert again.status_code == 200, again.text
        assert again.json()["id"] == p["id"] and again.json()["description"] == "two"
        assert again.json()["brief_version"] == 1, "omitted brief = unchanged"
        # With a brief, the re-POST appends the next version.
        third = await c.post(
            "/pm/projects",
            json=await typed(c, {"title": "minted", "brief": {"notes": "v2"}, **origin}),
        )
        assert third.status_code == 200 and third.json()["brief_version"] == 2, third.text
        assert [x["id"] for x in (await c.get("/pm/projects")).json()] == [p["id"]]

    async def test_appends_number_and_list(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c, brief={"problem_statement": "v1"})
        r2 = await _append(c, p["id"], {"problem_statement": "v2"}, note="enriched")
        assert r2.status_code == 200, r2.text
        assert r2.json()["version"] == 2 and r2.json()["author_kind"] == "conveyor"
        assert r2.json()["brief"]["problem_statement"] == "v2"
        r3 = await _put(c, p["id"], brief={"problem_statement": "v3"})
        assert r3.status_code == 200, r3.text
        assert (r3.json()["brief_version"], r3.json()["brief_author_kind"]) == (3, "conveyor")
        # Re-sending an identical snapshot still appends (intentional).
        r4 = await _append(c, p["id"], {"problem_statement": "v3"}, note="re-affirmed")
        assert r4.json()["version"] == 4
        # A PUT without brief keeps the current version.
        assert (await _put(c, p["id"], description="d")).json()["brief_version"] == 4

        listed = await c.get(f"/pm/projects/{p['id']}/brief/versions")
        assert listed.headers["X-Total-Count"] == "4"
        rows = listed.json()
        assert [r["version"] for r in rows] == [4, 3, 2, 1]
        assert [r["author_kind"] for r in rows] == ["conveyor"] * 4
        assert rows[2]["note"] == "enriched"
        assert all("brief" not in r for r in rows), "the list carries no snapshots"
        page = (await c.get(f"/pm/projects/{p['id']}/brief/versions?limit=2&offset=1")).json()
        assert [r["version"] for r in page] == [3, 2]
        v2 = (await c.get(f"/pm/projects/{p['id']}/brief/versions/2")).json()
        assert v2["brief"]["problem_statement"] == "v2" and v2["project_id"] == p["id"]
        assert (await c.get(f"/pm/projects/{p['id']}/brief/versions/5")).status_code == 404

        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert (got["brief_version"], got["brief"]["problem_statement"]) == (4, "v3")
        assert got["brief_updated_at"] == rows[0]["created_at"]

    async def test_compare_and_set(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c)
        assert p["brief_version"] == 1
        again = await _append(c, p["id"], {"notes": "x"}, if_current_version=None)
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "brief_version_mismatch"
        assert "current brief version is 1" in again.text
        stale = await _append(c, p["id"], {"notes": "x"}, if_current_version=2)
        assert stale.status_code == 409 and "current brief version is 1" in stale.text
        ok = await _append(c, p["id"], {"notes": "2"}, if_current_version=1)
        assert ok.status_code == 200 and ok.json()["version"] == 2, ok.text
        no_check = await _append(c, p["id"], {"notes": "3"})
        assert no_check.json()["version"] == 3
        assert _strip_none((await c.get(f"/pm/projects/{p['id']}")).json()["brief"]) == {
            "notes": "3"
        }
        assert (await c.get(f"/pm/projects/{p['id']}/brief/versions")).headers[
            "X-Total-Count"
        ] == "3"

    async def test_concurrent_appends_never_duplicate(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c, brief={"notes": "v1"})
        results = await asyncio.gather(*(_append(c, p["id"], {"notes": f"n{i}"}) for i in range(6)))
        assert all(r.status_code == 200 for r in results), [r.text for r in results]
        assert sorted(r.json()["version"] for r in results) == [2, 3, 4, 5, 6, 7]
        cas = await asyncio.gather(
            *(_append(c, p["id"], {"notes": "c"}, if_current_version=7) for _ in range(4))
        )
        assert sorted(r.status_code for r in cas) == [200, 409, 409, 409]
        versions = [
            r["version"] for r in (await c.get(f"/pm/projects/{p['id']}/brief/versions")).json()
        ]
        assert versions == list(range(8, 0, -1))

    async def test_append_waits_for_the_project_row_lock(
        self, authenticated_client: AsyncClient, test_session_factory
    ):
        """Deterministic: a second append blocks on the first's row lock, then numbers after it."""
        from models.pm import Project
        from schemas.pm_intake import ProjectBrief
        from services import pm_project_brief_service as brief_svc

        p = await _project(authenticated_client, brief={"notes": "v1"})
        pid = uuid.UUID(p["id"])
        async with test_session_factory() as s1, test_session_factory() as s2:
            proj1 = (await s1.execute(select(Project).where(Project.id == pid))).scalar_one()
            proj2 = (await s2.execute(select(Project).where(Project.id == pid))).scalar_one()
            await s2.commit()  # s2 holds no snapshot of the row from before s1's change
            r1 = await brief_svc.stage_version(
                s1,
                proj1,
                ProjectBrief(notes="a"),
                author_kind="user",
                subject_id="s",
            )
            second = asyncio.create_task(
                brief_svc.stage_version(
                    s2,
                    proj2,
                    ProjectBrief(notes="b"),
                    author_kind="conveyor",
                    subject_id="s",
                )
            )
            done, _ = await asyncio.wait({second}, timeout=0.5)
            assert not done, "the second append must wait for the first's row lock"
            await s1.commit()
            r2 = await asyncio.wait_for(second, timeout=10)
            await s2.commit()
            assert (r1.version, r2.version) == (2, 3)

    async def test_unique_version_is_the_backstop(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        p = await _project(authenticated_client, brief={"notes": "v1"})
        with pytest.raises(IntegrityError, match="uq_pm_project_brief_versions_project_version"):
            await test_session.execute(
                text(
                    "INSERT INTO pm_project_brief_versions (id, tenant_id, project_id, version, "
                    "brief, author_kind, author_subject_id) SELECT :vid, "
                    "tenant_id, id, 1, '{}'::jsonb, 'user', 's' FROM pm_projects "
                    "WHERE id = :id"
                ),
                {"vid": str(uuid.uuid4()), "id": p["id"]},
            )
        await test_session.rollback()
        for bad, name in (("version", "ck_pm_project_brief_versions_version"),):
            with pytest.raises(IntegrityError, match=name):
                await test_session.execute(
                    text(
                        "INSERT INTO pm_project_brief_versions (id, tenant_id, project_id, "
                        "version, brief, author_kind, author_subject_id) "
                        "SELECT :vid, tenant_id, id, 0, '{}'::jsonb, 'user', 's' "
                        "FROM pm_projects WHERE id = :id"
                    ),
                    {"vid": str(uuid.uuid4()), "id": p["id"]},
                )
            await test_session.rollback()
            assert bad
        with pytest.raises(IntegrityError, match="ck_pm_project_brief_versions_author_kind"):
            await test_session.execute(
                text(
                    "INSERT INTO pm_project_brief_versions (id, tenant_id, project_id, version, "
                    "brief, author_kind, author_subject_id) SELECT :vid, "
                    "tenant_id, id, 9, '{}'::jsonb, 'robot', 's' FROM pm_projects "
                    "WHERE id = :id"
                ),
                {"vid": str(uuid.uuid4()), "id": p["id"]},
            )
        await test_session.rollback()

    async def test_author_subject_comes_from_the_principal(self, authenticated_client: AsyncClient):
        from auth import get_subject_id
        from main import app

        c = authenticated_client
        app.dependency_overrides[get_subject_id] = lambda: "alice"
        try:
            p = await _project(c, brief={"notes": "v1"})
            spoof = await c.post(
                f"/pm/projects/{p['id']}/brief/versions",
                json={"brief": {"notes": "x"}, "author_subject_id": "mallory"},
            )
            assert spoof.status_code == 422, spoof.text
            ok = await _append(c, p["id"], {"notes": "x"})
            assert ok.json()["author_subject_id"] == "alice"
            put = await _put(c, p["id"], brief={"notes": "y"})
            assert put.status_code == 200, put.text
        finally:
            app.dependency_overrides.pop(get_subject_id, None)
        rows = (await c.get(f"/pm/projects/{p['id']}/brief/versions")).json()
        assert [r["author_subject_id"] for r in rows] == ["alice"] * 3

    async def test_invalid_snapshot_is_422_and_appends_nothing(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await _project(c, brief={"notes": "v1"})
        for bad in (
            {"notes": "x", "extra": 1},
            {"constraints": [{"kind": "custom"}]},
            {"risks": [{}]},
            {"approvers": [""]},
        ):
            resp = await _append(c, p["id"], bad)
            assert resp.status_code == 422, resp.text
        for bad_body in (
            # author_kind is derived from the caller, never accepted from the body.
            {"brief": {"notes": "x"}, "author_kind": "user"},
            {"brief": {"notes": "x"}, "author_kind": "conveyor"},
            {"note": "n"},  # brief required
            {"brief": {"notes": "x"}, "note": "n" * 2001},
            {"brief": {"notes": "x"}, "if_current_version": 0},
        ):
            resp = await c.post(f"/pm/projects/{p['id']}/brief/versions", json=bad_body)
            assert resp.status_code == 422, (bad_body, resp.text)
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["brief_version"] == 1

    async def test_put_brief_null_is_refused(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c, brief={"notes": "v1"})
        resp = await _put(c, p["id"], brief=None)
        assert resp.status_code == 422, resp.text
        assert "brief_cannot_be_cleared" in resp.text
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert _strip_none(got["brief"]) == {"notes": "v1"}

    async def test_versions_go_with_the_project(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        c = authenticated_client
        p = await _project(c, brief={"notes": "v1"})
        await _append(c, p["id"], {"notes": "v2"})
        assert (await c.delete(f"/pm/projects/{p['id']}")).status_code == 200
        n = (
            await test_session.execute(
                text("SELECT count(*) FROM pm_project_brief_versions WHERE project_id = :id"),
                {"id": p["id"]},
            )
        ).scalar_one()
        assert n == 0


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_other_tenant_reads_writes_and_lists_nothing(
        self, test_client: AsyncClient, tenant_a_id: str, tenant_b_id: str
    ):
        a, b = _As(test_client, tenant_a_id), _As(test_client, tenant_b_id)
        pa = await _project(a, **copy.deepcopy({**FR_PAYLOAD, "priority": None}))
        pb = await _project(b, brief={"notes": "b-v1"})
        await _append(a, pa["id"], {"notes": "a-v2", "constraints": FR_BRIEF["constraints"]})

        assert (await b.get(f"/pm/projects/{pa['id']}")).status_code == 404
        assert (await _put(b, pa["id"], scope_level="file")).status_code == 404
        assert (await b.get(f"/pm/projects/{pa['id']}/brief/versions")).status_code == 404
        assert (await b.get(f"/pm/projects/{pa['id']}/brief/versions/1")).status_code == 404
        assert (await _append(b, pa["id"], {"notes": "x"})).status_code == 404
        assert (
            await _append(b, pa["id"], {"notes": "x"}, if_current_version=2)
        ).status_code == 404, "a CAS against another tenant's project must not reveal its version"

        # Same version numbers exist in both tenants; each sees only its own snapshot.
        a_v1 = (await a.get(f"/pm/projects/{pa['id']}/brief/versions/1")).json()
        b_v1 = (await b.get(f"/pm/projects/{pb['id']}/brief/versions/1")).json()
        assert a_v1["brief"]["problem_statement"] == FR_BRIEF["problem_statement"]
        assert _strip_none(b_v1["brief"]) == {"notes": "b-v1"}
        assert [p["id"] for p in (await b.get("/pm/projects")).json()] == [pb["id"]]
        assert "Checkout" not in (await b.get("/pm/projects")).text

        got = (await a.get(f"/pm/projects/{pa['id']}")).json()
        assert got["scope_level"] == "multi_repo" and got["brief_version"] == 2


# ---------------------------------------------------------------------------
# Constraints are immutable after v1 (fr ProjectBriefUpdateSchema omits them: "the
# conveyor must never be able to loosen the gate that checks its work")
# ---------------------------------------------------------------------------

V1_CONSTRAINTS = [
    "do not touch the payments SDK",
    {"kind": "no_new_dependencies"},
    {"kind": "no_files_touched", "globs": ["migrations/**"]},
]


def _immutable(resp) -> bool:
    return resp.status_code == 422 and resp.json()["error"]["code"] == "brief_constraints_immutable"


class TestConstraintsImmutable:
    async def _p(self, c) -> dict:
        return await _project(
            c, brief={"problem_statement": "v1", "constraints": copy.deepcopy(V1_CONSTRAINTS)}
        )

    async def _unchanged(self, c, pid: str) -> None:
        got = (await c.get(f"/pm/projects/{pid}")).json()
        assert got["brief_version"] == 1
        assert got["brief"]["constraints"] == V1_CONSTRAINTS
        assert (await c.get(f"/pm/projects/{pid}/brief/versions")).headers["X-Total-Count"] == "1"

    async def test_conveyor_version_dropping_a_constraint_is_422(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await self._p(c)
        dropped = {"problem_statement": "v2", "constraints": V1_CONSTRAINTS[:2]}
        resp = await _append(c, p["id"], dropped)
        assert _immutable(resp), resp.text
        # Dropping the key altogether is the same loosening.
        resp = await _append(c, p["id"], {"problem_statement": "v2"})
        assert _immutable(resp), resp.text
        await self._unchanged(c, p["id"])

    async def test_conveyor_version_editing_a_constraint_is_422(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await self._p(c)
        edited = copy.deepcopy(V1_CONSTRAINTS)
        edited[2]["globs"] = ["migrations/**", "docs/**"]
        resp = await _append(c, p["id"], {"constraints": edited})
        assert _immutable(resp), resp.text
        reordered = [V1_CONSTRAINTS[1], V1_CONSTRAINTS[0], V1_CONSTRAINTS[2]]
        resp = await _append(c, p["id"], {"constraints": reordered})
        assert _immutable(resp), resp.text
        tightened = [*copy.deepcopy(V1_CONSTRAINTS), {"kind": "no_dependency_upgrades"}]
        resp = await _append(c, p["id"], {"constraints": tightened})
        assert _immutable(resp), "adding is a change too: constraints are fixed by v1"
        await self._unchanged(c, p["id"])

    async def test_user_version_changing_constraints_is_422(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await self._p(c)
        changed = {"constraints": ["something else"]}
        resp = await _append(c, p["id"], changed)
        assert _immutable(resp), resp.text
        put = await _put(c, p["id"], brief=changed, scope_level="repo")
        assert _immutable(put), put.text
        # The refused PUT wrote nothing (the other intake fields neither).
        assert (await c.get(f"/pm/projects/{p['id']}")).json()["scope_level"] is None
        await self._unchanged(c, p["id"])

    async def test_identical_constraints_with_other_fields_changed_appends(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        p = await self._p(c)
        same = {"problem_statement": "v2", "notes": "n", "constraints": V1_CONSTRAINTS}
        resp = await _append(c, p["id"], copy.deepcopy(same))
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == 2
        put = await _put(c, p["id"], brief={**copy.deepcopy(same), "problem_statement": "v3"})
        assert put.status_code == 200, put.text
        assert put.json()["brief_version"] == 3
        assert put.json()["brief"]["constraints"] == V1_CONSTRAINTS

    async def test_v1_sets_constraints_freely_absent_equals_empty(
        self, authenticated_client: AsyncClient
    ):
        c = authenticated_client
        # Version 1 (only ever written on create) sets constraints freely.
        p = await _project(c, brief={"constraints": ["rule"]})
        assert (p["brief_version"], p["brief"]["constraints"]) == (1, ["rule"])
        # Normalised: absent = null = [] — a v1 without constraints may be followed by [].
        q = await _project(c, brief={"notes": "no rules"})
        assert (await _append(c, q["id"], {"notes": "x", "constraints": []})).status_code == 200
        assert (await _append(c, q["id"], {"notes": "y", "constraints": None})).status_code == 200
        assert _immutable(await _append(c, q["id"], {"constraints": ["late rule"]}))


# ---------------------------------------------------------------------------
# author_kind: ``user`` iff the call is fr-delegated, otherwise ``conveyor`` (George 2026-10-13)
# ---------------------------------------------------------------------------

FR_SA_TENANT = "0195f48b-d000-7000-8000-0000000000aa"
FR_SA_SUBJECT = "0195f48b-d000-7000-8000-0000000000c1"
CUSTOMER_TENANT = "0195f48b-d000-7000-8000-0000000000bb"
CONVEYOR_TENANT = "0195f48b-d000-7000-8000-0000000000cc"
CONVEYOR_SUBJECT = "0195f48b-d000-7000-8000-0000000000c9"


@pytest.fixture
def licences(monkeypatch):
    """Introspection by token: fr's service account (the only delegating subject) and an
    ordinary licence (the conveyor's, or anybody else's)."""
    import auth.middleware as auth_middleware

    payloads = {
        "fr-token": {
            "active": True,
            "tenant_id": FR_SA_TENANT,
            "subject_id": FR_SA_SUBJECT,
            "license_id": "lic-fr",
        },
        "conveyor-token": {
            "active": True,
            "tenant_id": CONVEYOR_TENANT,
            "subject_id": CONVEYOR_SUBJECT,
            "license_id": "lic-conveyor",
        },
    }

    test_aware = auth_middleware._introspect_license  # conftest: plain X-Tenant-Id licences

    async def _fake(token: str) -> dict | None:
        return payloads.get(token) or await test_aware(token)

    monkeypatch.setattr(auth_middleware, "_introspect_license", _fake)
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", FR_SA_SUBJECT)


DELEGATED = {
    "X-Tenant-Id": "Bearer fr-token",
    "X-Effective-Tenant-Id": CUSTOMER_TENANT,
    "X-Subject-Id": "customer-user",
}
# fr acting for a user of fr's OWN tenant: the effective tenant equals the credential's.
DELEGATED_SAME_TENANT = {
    "X-Tenant-Id": "Bearer fr-token",
    "X-Effective-Tenant-Id": FR_SA_TENANT,
    "X-Subject-Id": "own-tenant-user",
}
FR_OWN_TENANT_NAME = "fr-own-tenant"
LICENCE = {"X-Tenant-Id": "Bearer conveyor-token"}


async def _kinds(c: AsyncClient, headers: dict) -> tuple[dict, list[str]]:
    """Create (v1), append (v2) and PUT (v3) a brief as ``headers``; the project and the
    author kind of every version, newest first.

    A fr-delegated caller (v1 ``user``) may only create: v4 ruling 1 (D15) refuses its v2
    and v3 with 403 ``project_writes_are_pm_only``, so it ends with v1 alone."""
    created = await c.post(
        "/pm/projects",
        json=await typed(c, {"title": "k", "brief": {"notes": "v1"}}, headers),
        headers=headers,
    )
    assert created.status_code == 200, created.text
    p = created.json()
    delegated = p["brief_author_kind"] == "user"
    v2 = await c.post(
        f"/pm/projects/{p['id']}/brief/versions", json={"brief": {"notes": "v2"}}, headers=headers
    )
    v3 = await c.put(
        f"/pm/projects/{p['id']}", json={"title": "k", "brief": {"notes": "v3"}}, headers=headers
    )
    for resp in (v2, v3):
        if delegated:
            assert resp.status_code == 403, resp.text
            assert _code(resp) == "project_writes_are_pm_only"
        else:
            assert resp.status_code == 200, resp.text
    rows = (await c.get(f"/pm/projects/{p['id']}/brief/versions", headers=headers)).json()
    assert [r["version"] for r in rows] == ([1] if delegated else [3, 2, 1])
    got = (await c.get(f"/pm/projects/{p['id']}", headers=headers)).json()
    assert got["brief_author_kind"] == rows[0]["author_kind"]
    assert "author_kind_source" not in rows[0] and "brief_author_kind_source" not in got
    return p, [r["author_kind"] for r in rows]


async def _subjects(c: AsyncClient, p: dict, headers: dict) -> list[str]:
    rows = (await c.get(f"/pm/projects/{p['id']}/brief/versions", headers=headers)).json()
    return [r["author_subject_id"] for r in rows]


@pytest.mark.usefixtures("licences")
class TestAuthorKindFromPrincipal:
    async def test_delegated_cross_tenant_is_user(self, test_client: AsyncClient):
        p, kinds = await _kinds(test_client, DELEGATED)
        assert p["tenant_id"] == CUSTOMER_TENANT
        assert kinds == ["user"]
        one = (
            await test_client.get(f"/pm/projects/{p['id']}/brief/versions/1", headers=DELEGATED)
        ).json()
        assert (one["author_kind"], one["author_subject_id"]) == ("user", "customer-user")

    async def test_delegated_same_tenant_is_user(self, test_client: AsyncClient):
        p, kinds = await _kinds(test_client, DELEGATED_SAME_TENANT)
        assert p["tenant_id"] == FR_SA_TENANT, "tenant resolution is unchanged"
        assert kinds == ["user"]
        assert await _subjects(test_client, p, DELEGATED_SAME_TENANT) == ["own-tenant-user"]

    @pytest.mark.parametrize("spelling", [FR_SA_TENANT.upper(), FR_OWN_TENANT_NAME])
    async def test_delegated_same_tenant_any_spelling_is_user_with_the_same_subject(
        self, test_client: AsyncClient, monkeypatch, spelling: str
    ):
        """fr's own tenant as a NAME or an upper-case uuid: same principal as the canonical.

        The forwarded ``X-License-Id`` is what tells the two branches apart: the
        cross-tenant branch swaps it in as the billing key, the same-tenant branch keeps
        fr's own licence. Tenant, kind and subject alone come out identical either way,
        so without the licence assertion this test could not fail if the resolved-uuid
        comparison were removed."""
        import auth.middleware as auth_middleware
        import auth.tenant_resolution as tenant_resolution

        def _registry(request):
            seg = request.url.path.rsplit("/", 2)[-2]
            return Response(
                200,
                json={"tenant_id": FR_SA_TENANT if seg == FR_OWN_TENANT_NAME else seg},
            )

        monkeypatch.setattr(
            tenant_resolution, "_client", AsyncClient(transport=MockTransport(_registry))
        )
        published: list[tuple[str, bool, str | None]] = []
        real_apply = auth_middleware._apply_principal

        def _spy(request, principal):
            real_apply(request, principal)
            published.append((principal.tenant_id, principal.delegated, request.state.license_id))

        monkeypatch.setattr(auth_middleware, "_apply_principal", _spy)
        h = {**DELEGATED_SAME_TENANT, "X-Effective-Tenant-Id": spelling, "X-License-Id": "lic-fwd"}
        p, kinds = await _kinds(test_client, h)
        assert p["tenant_id"] == FR_SA_TENANT
        assert kinds == ["user"]
        assert await _subjects(test_client, p, h) == ["own-tenant-user"]
        # Same principal as the canonical spelling, billing key included: fr's own
        # licence, never the forwarded one (that is the cross-tenant branch's shape).
        assert published, "the spy saw no request"
        assert set(published) == {(FR_SA_TENANT, True, "lic-fr")}

    async def test_delegated_same_tenant_without_x_subject_id_falls_back(
        self, test_client: AsyncClient
    ):
        h = {k: v for k, v in DELEGATED_SAME_TENANT.items() if k != "X-Subject-Id"}
        p, kinds = await _kinds(test_client, h)
        assert kinds == ["user"]
        assert await _subjects(test_client, p, h) == [FR_SA_SUBJECT]

    @pytest.mark.parametrize("effective", [None, CONVEYOR_TENANT, CONVEYOR_TENANT.upper()])
    async def test_non_allowlisted_x_subject_id_is_not_trusted(
        self, test_client: AsyncClient, effective: str | None
    ):
        h = {**LICENCE, "X-Subject-Id": "spoofed-user"}
        if effective is not None:
            h["X-Effective-Tenant-Id"] = effective
        p, kinds = await _kinds(test_client, h)
        assert kinds == ["conveyor"] * 3
        assert await _subjects(test_client, p, h) == [CONVEYOR_SUBJECT] * 3

    async def test_plain_licence_call_is_conveyor(self, test_client: AsyncClient):
        p, kinds = await _kinds(test_client, LICENCE)
        assert p["tenant_id"] == CONVEYOR_TENANT
        assert kinds == ["conveyor"] * 3

    async def test_fr_without_the_effective_tenant_header_is_conveyor(
        self, test_client: AsyncClient
    ):
        """Delegation is the header from an allowlisted subject, not the subject alone."""
        _, kinds = await _kinds(test_client, {"X-Tenant-Id": "Bearer fr-token"})
        assert kinds == ["conveyor"] * 3

    async def test_licence_with_fr_subject_but_no_effective_tenant_is_conveyor(
        self, test_client: AsyncClient
    ):
        """A licence whose subject is fr's, with no X-Effective-Tenant-Id, is not fr."""
        h = {"X-Tenant-Id": T("author-kind-tokenless"), "X-Subject-Id": FR_SA_SUBJECT}
        _, kinds = await _kinds(test_client, h)
        assert kinds == ["conveyor"] * 3

    async def test_non_allowlisted_subject_with_own_effective_tenant_is_conveyor(
        self, test_client: AsyncClient
    ):
        """Same-tenant: a subject outside AUTH_DELEGATING_SUBJECTS is not fr."""
        p, kinds = await _kinds(test_client, {**LICENCE, "X-Effective-Tenant-Id": CONVEYOR_TENANT})
        assert p["tenant_id"] == CONVEYOR_TENANT
        assert kinds == ["conveyor"] * 3

    async def test_non_allowlisted_subject_with_other_effective_tenant_is_refused(
        self, test_client: AsyncClient
    ):
        """Cross-tenant from a non-delegating subject is 403. It used to be served as
        the licence's own tenant (written as ``conveyor``) — a silent downgrade onto a
        tenant other than the one the caller asked for."""
        headers = {**LICENCE, "X-Effective-Tenant-Id": CUSTOMER_TENANT}
        resp = await test_client.post(
            "/pm/projects",
            json={"title": "k", "brief": {"notes": "v1"}},
            headers=headers,
        )
        assert resp.status_code == 403, resp.text

    async def test_the_body_cannot_name_the_author(self, test_client: AsyncClient):
        created = await test_client.post(
            "/pm/projects",
            json=await typed(test_client, {"title": "b", "brief": {"notes": "v1"}}, LICENCE),
            headers=LICENCE,
        )
        p = created.json()
        lie = await test_client.post(
            f"/pm/projects/{p['id']}/brief/versions",
            json={"brief": {"notes": "v2"}, "author_kind": "user"},
            headers=LICENCE,
        )
        assert lie.status_code == 422, lie.text
        rows = (
            await test_client.get(f"/pm/projects/{p['id']}/brief/versions", headers=LICENCE)
        ).json()
        assert [r["author_kind"] for r in rows] == ["conveyor"]


async def test_author_kind_source_column_is_gone(test_session: AsyncSession):
    cols = (
        (
            await test_session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pm_project_brief_versions' AND table_schema = "
                    "current_schema()"
                )
            )
        )
        .scalars()
        .all()
    )
    assert "author_kind" in cols and "author_subject_id" in cols
    assert "author_kind_source" not in cols


# ---------------------------------------------------------------------------
# Approvers are strings (fr keeps non-UUID approvers: routes/projects.ts:1953-1962)
# ---------------------------------------------------------------------------


async def test_named_approver_is_accepted(authenticated_client: AsyncClient):
    c = authenticated_client
    approvers = ["Ana Owner", "ana@acme.test", APPROVER]
    p = await _project(c, brief={"approvers": approvers})
    assert p["brief"]["approvers"] == approvers
    got = (await c.get(f"/pm/projects/{p['id']}/brief/versions/1")).json()
    assert got["brief"]["approvers"] == approvers


# ---------------------------------------------------------------------------
# A system-shared project read by another tenant carries no intake
# ---------------------------------------------------------------------------

REDACTED_FIELDS = INTAKE_OUT_FIELDS


class TestForeignReadRedaction:
    async def test_system_published_project_shows_no_intake_to_another_tenant(
        self, test_client: AsyncClient, test_session: AsyncSession, monkeypatch
    ):
        """The test DB role BYPASSES RLS, so only the application-level redaction stands
        between a platform tenant and the system tenant's intake here."""
        from settings import get_settings

        s = get_settings()
        system, reader = s.system_tenant_id, T("intake-platform-reader")
        monkeypatch.setattr(s, "platform_tenant_ids", f"{s.platform_tenant_ids},{reader}")
        sys_c, rd = _As(test_client, system), _As(test_client, reader)
        p = await _project(sys_c, **copy.deepcopy({**FR_PAYLOAD, "priority": None}))
        await test_session.execute(
            text("UPDATE pm_projects SET version_status = 'PUBLISHED' WHERE id = :id"),
            {"id": p["id"]},
        )
        await test_session.commit()

        own = (await sys_c.get(f"/pm/projects/{p['id']}")).json()
        assert own["brief"] is not None and own["repositories"] is not None
        assert own["github_token_secret_id"] == SECRET

        got = await rd.get(f"/pm/projects/{p['id']}")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["is_system"] is True and body["title"] == FR_PAYLOAD["title"]
        leaked = {k: body[k] for k in REDACTED_FIELDS if body[k] is not None}
        assert leaked == {}, leaked
        listed = [x for x in (await rd.get("/pm/projects")).json() if x["id"] == p["id"]]
        assert len(listed) == 1
        leaked = {k: listed[0][k] for k in REDACTED_FIELDS if listed[0][k] is not None}
        assert leaked == {}, leaked
        # Consistent with the versions routes, which are own-tenant only.
        assert (await rd.get(f"/pm/projects/{p['id']}/brief/versions")).status_code == 404
        assert (await rd.get(f"/pm/projects/{p['id']}/brief/versions/1")).status_code == 404

    async def test_current_brief_never_pairs_a_project_with_another_tenants_version(
        self, test_client: AsyncClient, test_session: AsyncSession, tenant_a_id, tenant_b_id
    ):
        a = _As(test_client, tenant_a_id)
        p = await _project(a, brief={"notes": "a-v1"})
        # A version row under ANOTHER tenant for A's project, made current. RLS would hide it
        # from A; the test role bypasses RLS, so only the explicit tenant predicate does.
        await test_session.execute(
            text(
                "INSERT INTO pm_project_brief_versions (id, tenant_id, project_id, version, "
                "brief, author_kind, author_subject_id) VALUES (:vid, "
                ":other, :id, 9, '{\"notes\": \"b-secret\"}'::jsonb, 'user', 's')"
            ),
            {"vid": str(uuid.uuid4()), "other": tenant_b_id, "id": p["id"]},
        )
        await test_session.execute(
            text("UPDATE pm_projects SET current_brief_version = 9 WHERE id = :id"),
            {"id": p["id"]},
        )
        await test_session.commit()
        got = (await a.get(f"/pm/projects/{p['id']}")).json()
        assert "b-secret" not in str(got)
        assert got["brief"] is None and got["brief_author_kind"] is None

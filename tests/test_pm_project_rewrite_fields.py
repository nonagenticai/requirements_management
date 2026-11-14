"""Rewrite projects (py2rust RS-D1 / RS-D6): source repos pinned to a sha, one target repo and
the target language — strictly additive (George R3). The ``max_cards`` cap George never asked
for was removed on 2026-11-07 (migration 0084); the same day the target language moved off
``it_inputs`` and onto the brief (migration 0085) — it is a fact about the REQUEST, and the
rewrite flows read ``brief.target_language``.

The DB tests hit real PostgreSQL through the FastAPI test client; the schema tests are pure.
"""

import copy

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.pm_intake import ItInputs, ProjectBrief, ProjectRepository, rewrite_violation
from services.pm_project_board_service import _primary_repository
from tests.briefs import MIN_BRIEF
from tests.project_types import typed
from tests.test_pm_project_intake import (
    FR_IT_INPUTS,
    FR_PAYLOAD,
    _levels,
    _project,
    _put,
    _strip_none,
)

SHA_A = "a" * 40
SHA_B = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret

SOURCES = [
    {"url": "https://github.com/acme/py-core", "role": "source", "sha": SHA_A},
    {"url": "https://github.com/acme/py-cli", "role": "source", "sha": SHA_B, "name": "cli"},
]
TARGET = {"url": "https://github.com/acme/rs", "branch": "main", "role": "target", "primary": True}
REWRITE_IT = {"it_flow": "rewrite"}
REWRITE_BRIEF = {**MIN_BRIEF, "target_language": "rust"}


def _rewrite(**over) -> dict:
    body = {
        "repositories": [TARGET, *SOURCES],
        "it_inputs": dict(REWRITE_IT),
        "brief": copy.deepcopy(REWRITE_BRIEF),
    }
    body.update(over)
    return copy.deepcopy(body)


# ---------------------------------------------------------------------------
# Schema (no DB)
# ---------------------------------------------------------------------------


class TestSchema:
    def test_new_fields_default_to_none_and_are_not_stored(self):
        repo = ProjectRepository(url="https://x/y")
        assert repo.role is None and repo.sha is None
        assert repo.model_dump(mode="json", exclude_none=True) == {
            "url": "https://x/y",
            "primary": False,
        }
        it = ItInputs.model_validate(FR_IT_INPUTS)
        assert it.model_dump(mode="json", exclude_none=True) == FR_IT_INPUTS
        # The target language is a BRIEF field now and absent unless sent.
        brief = ProjectBrief(problem_statement="p")
        assert brief.target_language is None
        assert brief.snapshot() == {"problem_statement": "p"}

    @pytest.mark.parametrize(
        "sha",
        ["a" * 39, "a" * 41, "g" * 40, "", "abc", "a" * 39 + " x", "a" * 40 + "\n0"],
    )
    def test_bad_sha_is_refused(self, sha: str):
        with pytest.raises(ValidationError):
            ProjectRepository(url="https://x/y", sha=sha)

    def test_sha_is_normalised_to_lowercase(self):
        assert ProjectRepository(url="https://x/y", sha="ABCDEF" + "0" * 34).sha == (
            "abcdef" + "0" * 34
        )

    def test_bad_role_and_language_are_refused(self):
        with pytest.raises(ValidationError):
            ProjectRepository(url="https://x/y", role="mirror")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ProjectBrief(target_language="go")  # type: ignore[arg-type]

    def test_max_cards_is_gone_and_any_value_is_refused(self):
        """George 2026-11-07: the cap was never requested. ``extra=forbid`` is the proof it is
        not silently stored again."""
        for bad in (0, 1, 200, 201, -1):
            with pytest.raises(ValidationError):
                ItInputs(max_cards=bad)  # type: ignore[call-arg]

    def test_language_is_gone_from_it_inputs(self):
        """It moved to the brief; ``extra=forbid`` stops it creeping back into the knobs."""
        with pytest.raises(ValidationError):
            ItInputs(language="rust")  # type: ignore[call-arg]

    def test_rewrite_rules(self):
        def check(
            repos: list[dict] | None, it: dict, brief: dict | None = REWRITE_BRIEF
        ) -> str | None:
            v = rewrite_violation(
                [ProjectRepository.model_validate(r) for r in repos] if repos is not None else None,
                ItInputs.model_validate(it),
                ProjectBrief.model_validate(brief) if brief is not None else None,
            )
            return v[0] if v else None

        assert check([TARGET, *SOURCES], REWRITE_IT) is None
        assert check([TARGET], REWRITE_IT) == "rewrite_needs_source_repository"
        no_sha = {k: v for k, v in SOURCES[0].items() if k != "sha"}
        assert check([TARGET, no_sha], REWRITE_IT) == "rewrite_source_needs_sha"
        assert check(SOURCES, REWRITE_IT) == "rewrite_needs_exactly_one_target_repository"
        two = [TARGET, {**TARGET, "url": "https://x/z", "primary": False}, *SOURCES]
        assert check(two, REWRITE_IT) == "rewrite_needs_exactly_one_target_repository"
        assert (
            check([{**TARGET, "primary": False}, *SOURCES], REWRITE_IT)
            == "rewrite_target_must_be_primary"
        )
        # The language comes from the brief now; absent (or no brief at all) is the same 422.
        no_lang = {k: v for k, v in REWRITE_BRIEF.items() if k != "target_language"}
        assert check([TARGET, *SOURCES], REWRITE_IT, no_lang) == "rewrite_needs_language"
        assert check([TARGET, *SOURCES], REWRITE_IT, None) == "rewrite_needs_language"
        # Not a rewrite: nothing new is enforced, whatever the repos look like.
        assert check([], {"it_flow": "bug"}) is None
        assert check(SOURCES, {}) is None
        assert rewrite_violation(None, None) is None

    def test_board_repository_url_is_the_target(self):
        repos = [ProjectRepository.model_validate(r) for r in [*SOURCES, TARGET]]
        assert _primary_repository(repos).url == TARGET["url"]

    def test_board_repository_sha_is_the_primarys_pin(self):
        """``repository_sha`` describes the SAME repo as ``repository_url``/``_branch``."""
        pinned = {**TARGET, "sha": SHA_A}
        repos = [ProjectRepository.model_validate(r) for r in [*SOURCES, pinned]]
        assert _primary_repository(repos).sha == SHA_A
        assert _primary_repository([ProjectRepository(url="https://x/y")]).sha is None


# ---------------------------------------------------------------------------
# Through the API (real Postgres)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestApi:
    async def test_existing_fr_payload_round_trips_unchanged(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        """R3: a pre-rewrite payload is stored and returned exactly as before."""
        c = authenticated_client
        await _levels(c)
        created = await _project(c, **copy.deepcopy(FR_PAYLOAD))
        got = (await c.get(f"/pm/projects/{created['id']}")).json()
        assert got == created
        # The response carries no new key at all (not even as null): byte-identical shape.
        for r in got["repositories"]:
            assert set(r) == {"url", "branch", "path", "name", "primary"}
        assert "language" not in got["it_inputs"] and "max_cards" not in got["it_inputs"]
        assert _strip_none(got["repositories"]) == [
            FR_PAYLOAD["repositories"][0],
            {**FR_PAYLOAD["repositories"][1], "primary": False},
        ]
        assert _strip_none(got["it_inputs"]) == FR_IT_INPUTS
        row = (
            await test_session.execute(
                text("SELECT repositories, it_inputs FROM pm_projects WHERE id = :id"),
                {"id": created["id"]},
            )
        ).one()
        # No new key reaches storage for a payload that does not send one.
        assert all("role" not in r and "sha" not in r for r in row.repositories)
        assert row.it_inputs == FR_IT_INPUTS

    async def test_rewrite_payload_is_accepted_and_returned(
        self, authenticated_client: AsyncClient, test_session: AsyncSession
    ):
        c = authenticated_client
        created = await _project(c, **_rewrite())
        got = (await c.get(f"/pm/projects/{created['id']}")).json()
        assert got == created
        assert _strip_none(got["repositories"]) == [
            TARGET,
            {**SOURCES[0], "primary": False},
            {**SOURCES[1], "primary": False},
        ]
        assert _strip_none(got["it_inputs"]) == REWRITE_IT
        # The target language is stored on the brief, in version 1, and nowhere else.
        assert got["brief"]["target_language"] == "rust"
        assert "language" not in got["it_inputs"]
        row = (
            await test_session.execute(
                text("SELECT repositories, it_inputs FROM pm_projects WHERE id = :id"),
                {"id": created["id"]},
            )
        ).one()
        assert row.repositories[1] == {**SOURCES[0], "primary": False}
        assert row.it_inputs == REWRITE_IT

    @pytest.mark.parametrize(
        ("over", "code"),
        [
            ({"repositories": [TARGET]}, "rewrite_needs_source_repository"),
            (
                {"repositories": [TARGET, {"url": "https://x/s", "role": "source"}]},
                "rewrite_source_needs_sha",
            ),
            ({"repositories": SOURCES}, "rewrite_needs_exactly_one_target_repository"),
            (
                {"repositories": [{**TARGET, "primary": False}, *SOURCES]},
                "rewrite_target_must_be_primary",
            ),
            ({"brief": copy.deepcopy(MIN_BRIEF)}, "rewrite_needs_language"),
            ({"repositories": None}, "rewrite_needs_source_repository"),
        ],
    )
    async def test_malformed_rewrite_create_is_422_and_writes_nothing(
        self,
        authenticated_client: AsyncClient,
        test_session: AsyncSession,
        over: dict,
        code: str,
    ):
        c = authenticated_client
        before = (await test_session.execute(text("SELECT count(*) FROM pm_projects"))).scalar()
        body = await typed(
            c, {"title": "rw", "brief": copy.deepcopy(MIN_BRIEF), **_rewrite(**over)}
        )
        resp = await c.post("/pm/projects", json=body)
        assert resp.status_code == 422, resp.text
        assert code in resp.text
        after = (await test_session.execute(text("SELECT count(*) FROM pm_projects"))).scalar()
        assert after == before

    async def test_bad_sha_is_422(self, authenticated_client: AsyncClient):
        c = authenticated_client
        bad = [TARGET, {**SOURCES[0], "sha": "deadbeef"}]
        body = await typed(
            c, {"title": "rw", "brief": copy.deepcopy(MIN_BRIEF), **_rewrite(repositories=bad)}
        )
        resp = await c.post("/pm/projects", json=body)
        assert resp.status_code == 422, resp.text
        assert "sha_must_be_40_hex" in resp.text

    async def test_update_judges_the_merged_project(self, authenticated_client: AsyncClient):
        """PUT of one half is checked against the other, stored half."""
        c = authenticated_client
        p = await _project(c, **_rewrite())
        # Dropping the target's primary flag (repositories only; it_inputs kept) → refused.
        resp = await _put(c, p["id"], repositories=[{**TARGET, "primary": False}, *SOURCES])
        assert resp.status_code == 422 and "rewrite_target_must_be_primary" in resp.text
        # A brief version without the language (repositories kept) → refused.
        resp = await _put(c, p["id"], brief=copy.deepcopy(MIN_BRIEF))
        assert resp.status_code == 422 and "rewrite_needs_language" in resp.text
        # Sending the language in it_inputs is refused (extra=forbid) — it lives on the brief.
        resp = await _put(c, p["id"], it_inputs={**REWRITE_IT, "language": "rust"})
        assert resp.status_code == 422, resp.text
        assert "language" in resp.text
        # Sending the removed cap back is refused (extra=forbid), never quietly stored.
        resp = await _put(c, p["id"], it_inputs={**REWRITE_IT, "max_cards": 7})
        assert resp.status_code == 422, resp.text
        assert "max_cards" in resp.text
        # Turning a plain project into a rewrite without the repos is refused.
        plain = await _project(c, **copy.deepcopy({"repositories": FR_PAYLOAD["repositories"]}))
        resp = await _put(c, plain["id"], it_inputs=REWRITE_IT)
        assert resp.status_code == 422 and "rewrite_needs_source_repository" in resp.text

    async def test_non_rewrite_project_may_carry_the_new_fields(
        self, authenticated_client: AsyncClient
    ):
        """Outside ``it_flow: rewrite`` nothing new is enforced; the fields just round-trip."""
        c = authenticated_client
        repos = [{"url": "https://x/a", "role": "source", "primary": True}]
        p = await _project(c, repositories=repos, it_inputs={"it_flow": "bug"})
        assert _strip_none(p["repositories"]) == repos
        assert p["it_inputs"]["it_flow"] == "bug"
        assert "max_cards" not in p["it_inputs"]

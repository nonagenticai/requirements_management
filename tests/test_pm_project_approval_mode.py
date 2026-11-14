"""Per-project approval mode (George 2026-10-27): ``it_inputs.approval_mode``.

fr asks the user which conveyor sign-offs they want; RM stores the choice on the project
and returns it; n8n reads it. Absent = ``all`` (today's behaviour) and is not stored.
The DB tests hit real PostgreSQL through the FastAPI test client; the schema tests are pure.
"""

import copy
from typing import get_args

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from schemas.pm_intake import ApprovalMode, ItInputs
from tests.test_pm_project_intake import FR_IT_INPUTS, FR_PAYLOAD, _levels, _project, _put

MODES = ("all", "auto_drafts", "brief_and_closure", "autonomous")


class TestSchema:
    def test_the_four_modes_and_nothing_else(self):
        assert get_args(ApprovalMode) == MODES
        for mode in MODES:
            assert ItInputs(approval_mode=mode).approval_mode == mode
        for bad in ("never", "ALL", "", "auto-drafts", 1):
            with pytest.raises(ValidationError):
                ItInputs(approval_mode=bad)  # type: ignore[arg-type]

    def test_absent_is_none_and_not_dumped(self):
        it = ItInputs.model_validate(FR_IT_INPUTS)
        assert it.approval_mode is None
        assert "approval_mode" not in it.model_dump(mode="json")


@pytest.mark.asyncio
class TestApi:
    @pytest.mark.parametrize("mode", MODES)
    async def test_create_round_trips(self, authenticated_client: AsyncClient, mode: str):
        c = authenticated_client
        await _levels(c)
        body = copy.deepcopy(FR_PAYLOAD)
        body["it_inputs"] = {**FR_IT_INPUTS, "approval_mode": mode}
        created = await _project(c, **body)
        assert created["it_inputs"]["approval_mode"] == mode
        got = (await c.get(f"/pm/projects/{created['id']}")).json()
        assert got["it_inputs"]["approval_mode"] == mode
        assert got == created

    async def test_absent_is_not_returned(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c, it_inputs={"it_flow": "bug"})
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert "approval_mode" not in got["it_inputs"]

    async def test_update_changes_and_clears_it(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c, it_inputs={"it_flow": "bug", "approval_mode": "autonomous"})
        resp = await _put(c, p["id"], it_inputs={"it_flow": "bug", "approval_mode": "auto_drafts"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["it_inputs"]["approval_mode"] == "auto_drafts"
        resp = await _put(c, p["id"], it_inputs={"it_flow": "bug"})
        assert resp.status_code == 200, resp.text
        assert "approval_mode" not in resp.json()["it_inputs"]

    async def test_unknown_mode_is_422(self, authenticated_client: AsyncClient):
        c = authenticated_client
        p = await _project(c, it_inputs={"it_flow": "bug"})
        resp = await _put(c, p["id"], it_inputs={"it_flow": "bug", "approval_mode": "never"})
        assert resp.status_code == 422, resp.text
        got = (await c.get(f"/pm/projects/{p['id']}")).json()
        assert "approval_mode" not in got["it_inputs"]

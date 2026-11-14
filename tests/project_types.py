"""A project type for tests that create a project (George 10-13 v4 ruling 2 / D16).

``POST /pm/projects`` requires a ``project_type_id`` (422 ``project_type_required``) whose
type selects a ``setup`` phase (409 ``project_type_missing_setup_phase``): every project
starts in ``setup``. Tests that only need *a* project use this minimal type — one phase,
``setup`` — created in the caller's tenant catalogue on first use (idempotent by name).
"""

from typing import Any

TEST_TYPE = "test_setup_only"


async def _ok(resp) -> Any:
    assert resp.status_code == 200, resp.text
    return resp.json()


async def setup_type_id(c: Any, headers: dict | None = None, name: str = TEST_TYPE) -> str:
    """The id of the caller's minimal project type (``[setup]``), created if missing."""
    kw = {"headers": headers} if headers else {}
    types = await _ok(await c.get("/pm/project-types", **kw))
    found = next((t for t in types if t["name"] == name), None)
    if found:
        return found["id"]
    phases = await _ok(await c.get("/pm/project-phases", **kw))
    setup = next((p for p in phases if p["name"] == "setup"), None)
    if setup is None:
        setup = await _ok(
            await c.post(
                "/pm/project-phases",
                json={"name": "setup", "description": "every project starts here"},
                **kw,
            )
        )
    created = await _ok(
        await c.post("/pm/project-types", json={"name": name, "phase_ids": [setup["id"]]}, **kw)
    )
    return created["id"]


async def typed(c: Any, body: dict[str, Any], headers: dict | None = None) -> dict[str, Any]:
    """``body`` plus the caller's minimal ``project_type_id``, unless it carries one."""
    if "project_type_id" in body:
        return body
    return {"project_type_id": await setup_type_id(c, headers), **body}

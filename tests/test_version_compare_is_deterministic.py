"""A version string does not identify one snapshot — compare must still be deterministic.

``EntityService._record_snapshot`` writes a row on every update and copies
``entity.version`` verbatim, so an entity updated N times without a version bump
has N history rows all carrying the same version string.

Measured on production 2026-09-04: ``entity_version_history`` holds 39,707 rows,
**39,687 of them version "1.0.0"**; 1,480 entities hold more than one snapshot
under a single version and one architecture element holds **186**.

``VersionService.compare`` selected each side with a bare ``.first()`` and no
``ORDER BY``, so which of those 186 rows represented "1.0.0" was arbitrary.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from models.version_history import EntityVersionHistory
from services.version_service import VersionService

ENTITY_TYPE = "architecture_element"


async def _snapshot(session, entity_id, version: str, title: str) -> None:
    session.add(
        EntityVersionHistory(
            entity_type=ENTITY_TYPE,
            entity_id=entity_id,
            version=version,
            version_status="updated",
            snapshot_data={"title": title},
        )
    )
    await session.flush()


class TestCompareIsDeterministic:
    async def test_newest_snapshot_of_a_repeated_version_wins(self, test_session) -> None:
        """KILLING MUTATION: drop the ``.order_by(created_at desc, id desc)``.

        Without it the query matches three rows and ``.first()`` returns
        whichever Postgres hands back — in practice the oldest (physical order),
        so the diff names ``oldest`` instead of ``newest`` and this fails.
        """
        entity_id = uuid4()
        for title in ("oldest", "middle", "newest"):
            await _snapshot(test_session, entity_id, "1.0.0", title)
        await _snapshot(test_session, entity_id, "2.0.0", "next-major")

        result = await VersionService().compare(
            test_session, ENTITY_TYPE, entity_id, "1.0.0", "2.0.0"
        )

        assert result.version_diff["title"] == "'newest' -> 'next-major'"

    async def test_repeated_calls_agree(self, test_session) -> None:
        """The same request must answer the same way every time.

        KILLING MUTATION: same as above. This is the property the missing
        ORDER BY actually violated — a single call cannot reveal it.
        """
        entity_id = uuid4()
        for title in ("a", "b", "c", "d"):
            await _snapshot(test_session, entity_id, "1.0.0", title)
        await _snapshot(test_session, entity_id, "3.0.0", "later")

        svc = VersionService()
        seen = {
            (
                await svc.compare(test_session, ENTITY_TYPE, entity_id, "1.0.0", "3.0.0")
            ).version_diff["title"]
            for _ in range(5)
        }
        assert seen == {"'d' -> 'later'"}

    async def test_missing_version_still_404s(self, test_session) -> None:
        """Vacuity floor: the 404 path must survive the ordering change.

        KILLING MUTATION: make ``_canonical`` ignore its ``version`` argument —
        every version would then resolve and this stops raising.
        """
        from fastapi import HTTPException

        entity_id = uuid4()
        await _snapshot(test_session, entity_id, "1.0.0", "only")

        with pytest.raises(HTTPException) as exc:
            await VersionService().compare(test_session, ENTITY_TYPE, entity_id, "1.0.0", "9.9.9")
        assert exc.value.status_code == 404

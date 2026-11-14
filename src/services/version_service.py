"""Shared version endpoint logic for principles and decisions.

Replaces the ~130 lines of identical version code that was duplicated
between the principles and decisions routers.
"""

import os
from uuid import UUID

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.version_history import EntityVersionHistory
from schemas.common import VersionComparison, VersionInfo
from version_utils import compare_versions, is_breaking_change

logger = structlog.get_logger(__name__)

# Upper bound on the rows ``list_versions`` will read back for one entity.
#
# This is defence against unbounded growth, nothing more — it is NOT a fix for
# an OOM and must not be described as one. Measured on production 2026-09-05:
# the largest single entity holds **206** snapshots, ``snapshot_data`` averages
# 344 bytes and peaks at 1,818, so today's worst case is ~370 KB. The cap of 500
# is ~2.4x the measured maximum and is never reached by any entity that exists.
#
# ⛔ Deliberately a LIMIT and not a raise. The bounded helpers in
# ``repositories/bounded.py`` raise ``InputTooLarge`` (a plain ``ValueError``),
# which only becomes a 422 through a per-router ``route_class``. Six routers
# call this method and ``architecture_standards``/``architecture_views`` are
# bare ``APIRouter()`` with no such class and no local handler, so the raise
# would fall through to the catch-all in ``main.py`` and ship a 500 on a read
# that has nothing wrong with it. Truncating and saying so in the log is the
# behaviour that degrades gracefully.
# ⛔ Floored at 1. ``.limit(0)`` returns no rows, and ``list_versions`` reads an
# empty result as "this entity has no history" and synthesises a single
# current-version entry — so a mis-set env var would make EVERY entity report
# exactly one version, with no error anywhere. ``repositories/bounded.py``
# refuses a cap below 1 for the same reason; this now matches it.
MAX_VERSION_HISTORY_ROWS = max(1, int(os.getenv("MAX_VERSION_HISTORY_ROWS", "500")))


class VersionService:
    async def get_info(self, entity) -> VersionInfo:
        return VersionInfo(
            version=entity.version,
            version_status=entity.version_status,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            approved_by=entity.approved_by,
            approved_at=entity.approved_at,
            published_at=entity.published_at,
        )

    async def list_versions(
        self,
        session: AsyncSession,
        entity_type: str,
        entity_id: UUID,
        entity,
    ) -> list[VersionInfo]:
        """Version history for one entity, newest first, capped.

        ⚠️ THE PREFIX IS DELIBERATELY UNLABELLED, and that is a trade-off, not an
        oversight. ``repositories/bounded.py``'s rule is that truncating an
        OUTPUT is honest only when the caller gets a *labelled* prefix. Six
        routers call this method (architecture_elements, architecture_standards,
        architecture_views, decisions, principles, requirements) and not one of
        them exposes ``limit``/``offset`` or sets ``X-Total-Count``, so labelling
        it means changing six public response contracts. Measured 2026-09-05, no
        entity is anywhere near the cap — the largest holds 206 snapshots against
        a cap of 500 — so the label would describe a case that does not occur.
        The ``version_history_truncated`` warning is the tripwire: if it ever
        fires, THAT is the trigger to add real pagination and a header, and this
        note is the record of the decision.
        """
        query = (
            select(EntityVersionHistory)
            .where(
                EntityVersionHistory.entity_type == entity_type,
                EntityVersionHistory.entity_id == entity_id,
            )
            # ⚠️ THE TIEBREAKERS ARE LOAD-BEARING, NOT DECORATION. ``version`` is a
            # plain string column and it is massively non-unique: the comment
            # below measures 39,687 of 39,707 rows sitting at "1.0.0", one entity
            # holding 186 snapshots under a single version string. ``ORDER BY
            # version DESC`` alone is therefore not a total order, so once an
            # entity exceeds the cap Postgres may return a DIFFERENT prefix on
            # every call. ``compare()`` already solved this exact ambiguity in
            # ``_canonical`` with the same two tiebreakers; this is that fix
            # applied to the list path.
            .order_by(
                EntityVersionHistory.version.desc(),
                EntityVersionHistory.created_at.desc(),
                EntityVersionHistory.id.desc(),
            )
            # +1 is a TRUNCATION PROBE, not an off-by-one: reading one more row
            # than the cap is what distinguishes "exactly at the cap" from
            # "there was more". Comparing ``len(rows) >= cap`` instead would log
            # a truncation for an entity holding exactly ``cap`` versions, which
            # is a false alarm in the log that the bound exists to keep honest.
            .limit(MAX_VERSION_HISTORY_ROWS + 1)
        )
        result = await session.execute(query)
        versions = result.scalars().all()
        truncated = len(versions) > MAX_VERSION_HISTORY_ROWS
        if truncated:
            versions = versions[:MAX_VERSION_HISTORY_ROWS]
            # At the cap the caller is seeing a prefix of the history, not all of
            # it. Log rather than fail so truncation is observable instead of
            # silent; the response shape is unchanged.
            logger.warning(
                "version_history_truncated",
                entity_type=entity_type,
                entity_id=str(entity_id),
                limit=MAX_VERSION_HISTORY_ROWS,
                returned=len(versions),
            )
        if not versions:
            return [
                VersionInfo(
                    version=entity.version,
                    version_status=entity.version_status,
                    created_at=entity.created_at,
                    updated_at=entity.updated_at,
                    approved_by=entity.approved_by,
                    approved_at=entity.approved_at,
                    published_at=entity.published_at,
                )
            ]
        return [
            VersionInfo(
                version=v.version,
                version_status=v.version_status,
                created_at=v.created_at,
                updated_at=None,
                approved_by=(v.snapshot_data.get("approved_by") if v.snapshot_data else None),
                approved_at=(v.snapshot_data.get("approved_at") if v.snapshot_data else None),
                published_at=(v.snapshot_data.get("published_at") if v.snapshot_data else None),
            )
            for v in versions
        ]

    async def compare(
        self,
        session: AsyncSession,
        entity_type: str,
        entity_id: UUID,
        v1: str,
        v2: str,
    ) -> VersionComparison:
        try:
            comparison_result = compare_versions(v1, v2)
            is_breaking = is_breaking_change(v1, v2)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

        # ⚠️ A version string does NOT identify one snapshot. A snapshot is
        # recorded on every update and copies ``entity.version`` verbatim
        # (``EntityService._record_snapshot``), so an entity updated N times
        # without a version bump has N rows all carrying the same version.
        # Measured on production 2026-09-04: 39,687 of 39,707 rows are
        # "1.0.0", 1,480 entities hold more than one snapshot under a single
        # version, and one element holds 186. Selecting with a bare
        # ``.first()`` and no ORDER BY therefore returned an ARBITRARY row —
        # the same request could answer differently run to run, and Postgres
        # is under no obligation to be consistent.
        #
        # The newest snapshot bearing a version is its final state, so that is
        # the canonical one to compare. Ordering is total (``created_at`` then
        # the uuid7 primary key, which is itself time-ordered) so ties cannot
        # reintroduce the ambiguity.
        def _canonical(version: str):
            return (
                select(EntityVersionHistory)
                .where(
                    EntityVersionHistory.entity_type == entity_type,
                    EntityVersionHistory.entity_id == entity_id,
                    EntityVersionHistory.version == version,
                )
                .order_by(
                    EntityVersionHistory.created_at.desc(),
                    EntityVersionHistory.id.desc(),
                )
                .limit(1)
            )

        snap1_query = _canonical(v1)
        snap2_query = _canonical(v2)
        snap1 = (await session.execute(snap1_query)).scalars().first()
        snap2 = (await session.execute(snap2_query)).scalars().first()

        if not snap1 or not snap2:
            raise HTTPException(
                status_code=404,
                detail=f"Version snapshot not found for one or both versions: {v1}, {v2}",
            )

        version_diff: dict[str, str] = {}
        data1 = snap1.snapshot_data or {}
        data2 = snap2.snapshot_data or {}
        all_keys = set(data1.keys()) | set(data2.keys())
        for key in sorted(all_keys):
            old_val = data1.get(key)
            new_val = data2.get(key)
            if old_val != new_val:
                version_diff[key] = f"{old_val!r} -> {new_val!r}"

        return VersionComparison(
            old_version=v1,
            new_version=v2,
            is_breaking_change=is_breaking,
            comparison_result=comparison_result,
            version_diff=version_diff,
        )

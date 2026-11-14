"""Who wrote a brief version — derived from the PRINCIPAL, never declared (George 2026-10-13).

One rule: anything not done through fr IS the conveyor. ``author_kind`` is ``user`` if and
only if the request is fr-delegated — an introspected subject on
``AUTH_DELEGATING_SUBJECTS`` acting via ``X-Effective-Tenant-Id`` — and that subject is not
the PM itself. Every other call — a plain licence token, a non-allowlisted subject sending
the header — is ``conveyor``. No body value.

⭐ ``AUTH_PM_SUBJECTS`` (default EMPTY, ships inert) names the PM's own subject(s). The PM
(the conveyor / n8n) must delegate the tenant as well — one credential serves every tenant —
but it IS the PM, and the PM-only rule (``auth.pm_only``) exists to stop FR writing a
project's resources, not the conveyor. A delegated subject named here therefore stays
``author_kind=conveyor``. Measured 2026-11-08: the conveyor's ``Save brief version`` was 403
``project_writes_are_pm_only`` because its delegated call was classified as fr.

The subject used is ``delegator_subject_id`` — the INTROSPECTED subject — because a
delegated call may carry the end user's id in ``subject_id`` via ``X-Subject-Id``; a header
must never decide the classification.
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import Request

AuthorKind = Literal["user", "conveyor"]

#: Subjects that ARE the PM (the conveyor / n8n), even when they delegate a tenant.
#: Read at CALL time — the repo's staged-rollout idiom — so a ConfigMap change applies
#: without a module reload. Empty is the default and preserves the prior classification.
PM_SUBJECTS_ENV = "AUTH_PM_SUBJECTS"


def pm_subjects() -> frozenset[str]:
    """Subjects named as the PM. Empty by default (nothing changes until armed)."""
    raw = os.getenv(PM_SUBJECTS_ENV, "") or ""
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def author_kind_of(principal: object | None) -> AuthorKind:
    """``user`` iff the principal is fr-delegated; ``conveyor`` otherwise (incl. the PM)."""
    if getattr(principal, "delegated", False) is not True:
        return "conveyor"
    delegator = str(getattr(principal, "delegator_subject_id", "") or "")
    subject = delegator or str(getattr(principal, "subject_id", "") or "")
    if subject and subject in pm_subjects():
        return "conveyor"
    return "user"


async def get_author_kind(request: Request) -> AuthorKind:
    """FastAPI dependency: the author kind of the request's resolved principal."""
    return author_kind_of(getattr(request.state, "principal", None))

"""``author_kind``: the PM stays the conveyor when it delegates (``AUTH_PM_SUBJECTS``).

Measured 2026-11-08 (n8n exec 1218309, ``project_setup.DOING`` → ``Save brief version``):
the conveyor's requirements credential was 403 ``project_writes_are_pm_only`` because
``AUTH_DELEGATING_SUBJECTS`` — armed so ONE credential can serve every tenant — made every
delegated call ``author_kind=user``, i.e. fr. The PM-only rule exists to stop FR writing a
project's resources, not the PM itself.

The classification must key on the INTROSPECTED (delegator) subject, never on a forwarded
``X-Subject-Id``. Pure function; no DB, no app.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auth.author_kind import author_kind_of, pm_subjects
from auth.pm_only import PM_ONLY_CODE, require_pm_writer

PM_SUBJECT = "01a13f6f-43ec-7274-87f9-bd81dcdaac72"
FR_SUBJECT = "019ffe9f-31fd-70da-b3c5-f921f7290b01"
CUSTOMER_SUBJECT = "0195f48b-d000-7000-8000-0000000000c2"


@pytest.fixture(autouse=True)
def _no_pm_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the SHIPPED state, never ambient env."""
    monkeypatch.delenv("AUTH_PM_SUBJECTS", raising=False)


def _delegated(subject_id: str, delegator_subject_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        delegated=True, subject_id=subject_id, delegator_subject_id=delegator_subject_id
    )


def test_ships_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the list empty every delegated call is fr, exactly as before."""
    assert pm_subjects() == frozenset()
    assert author_kind_of(_delegated(CUSTOMER_SUBJECT, PM_SUBJECT)) == "user"


def test_pm_subject_delegating_is_the_conveyor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PM_SUBJECTS", PM_SUBJECT)
    assert author_kind_of(_delegated(CUSTOMER_SUBJECT, PM_SUBJECT)) == "conveyor"


def test_fr_subject_delegating_is_a_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PM_SUBJECTS", PM_SUBJECT)
    assert author_kind_of(_delegated(CUSTOMER_SUBJECT, FR_SUBJECT)) == "user"


def test_a_forwarded_subject_cannot_name_fr_as_the_pm(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Subject-Id is caller-supplied: the delegator is what classifies."""
    monkeypatch.setenv("AUTH_PM_SUBJECTS", PM_SUBJECT)
    # fr delegated; the end user's forwarded id happens to equal the PM subject.
    assert author_kind_of(_delegated(PM_SUBJECT, FR_SUBJECT)) == "user"


def test_a_non_delegated_caller_is_the_conveyor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PM_SUBJECTS", PM_SUBJECT)
    plain = SimpleNamespace(delegated=False, subject_id=PM_SUBJECT, delegator_subject_id="")
    assert author_kind_of(plain) == "conveyor"


def _request(principal: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


async def test_pm_only_guard_admits_the_delegating_pm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured failure: the conveyor's own write was refused as fr."""
    monkeypatch.setenv("AUTH_PM_SUBJECTS", PM_SUBJECT)
    await require_pm_writer(_request(_delegated(CUSTOMER_SUBJECT, PM_SUBJECT)))


async def test_pm_only_guard_still_refuses_delegating_fr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PM_SUBJECTS", PM_SUBJECT)
    with pytest.raises(HTTPException) as caught:
        await require_pm_writer(_request(_delegated(CUSTOMER_SUBJECT, FR_SUBJECT)))
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == PM_ONLY_CODE

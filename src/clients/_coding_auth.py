"""Outbound headers for calls into the ``coding-*`` fleet.

Used by ``coding_analysis.py`` (formerly also ``coding_ai.py``, removed). Both
used to build ``{"X-Tenant-Id": tenant_id}`` inline, which is how the two drifted
into the same defect at the same time: neither presented a credential, and both
started 401ing the moment coding stopped trusting a bare header
(``TRUST_TENANT_HEADER=false``, live on all nine coding services since
2026-08-14). Measured 2026-08-15 by driving both calls from the requirements pod:
``/api/ai/architecture/proposals`` → 401 and
``/api/analysis/architecture/components/{c}`` → 401.

WHY TWO HEADERS AND NOT ONE
---------------------------
The two carry different claims and must not be collapsed:

* ``X-Tenant-Id`` — *whose data we are asking for*. A **customer** tenant, taken
  from the inbound request. Unverified by construction; it is an assertion.
* ``Authorization`` — *who is asking*. This service's own licence token. Verified
  downstream by license-introspect.

coding honours the asserted tenant only because the Authorization subject
(``svc-requirements``) is listed in coding-analysis's
``CODING_ONBEHALF_SUBJECTS`` (gitops#4944 removed it from coding-ai's on
2026-10-27). Putting the service token into ``X-Tenant-Id`` instead — which
is what ``outbound_headers(service_token=...)`` does, and the mistake this
module exists to prevent — would resolve every request to the
*service's* tenant and silently serve the wrong customer's data with a 200.

WHY THE DEDICATED ARM AND NOT ``extra=``
----------------------------------------
This used ``extra={"Authorization": ...}`` because the installed kag-common wheel
(0.15.0) proved to have no ``authorization_service_token`` parameter — the source
tree was newer than the wheel, and ``ty`` caught the mismatch. The vendored
``fleet_auth`` is copied from 0.37.0, which has the dedicated on-behalf-of arm,
so this module now names it: same wire bytes, one fewer hand-built dict, and the
parameter that exists for exactly this shape is the one being used.

ATTRIBUTION: ``X-App-Id``
------------------------
``app_id`` names WHICH application asked, and ``fleet_auth.outbound_headers``
emits it as ``X-App-Id``. Attribution was structurally impossible on the wheel
this service ran: the shared helper had no ``app_id`` parameter at all
(kag_common#49), so no caller could assert one and this call sent none. The value
is this service's catalogue app id, ``settings.requirements_module_app_id`` — the
same id former LLM-lane calls sent, restored rather than invented (see the
field). It is never defaulted inside the header builder: a self-named app_id that
overwrites a relayed one is worse than an absent header.
"""

from __future__ import annotations

from fleet_auth import outbound_headers


def coding_headers(tenant_id: str) -> dict[str, str]:
    """Build headers for one outbound coding-* call on behalf of ``tenant_id``.

    ``tenant_id`` is the **customer's** tenant and is sent bare. This service's
    own identity rides ``Authorization`` and is read at call time (not captured
    at import) so a rotated token takes effect on the next request.

    When ``CODING_SERVICE_TOKEN`` is unset the Authorization header is simply
    absent — the call degrades to the pre-2026-08-15 header-only shape, which
    coding answers with 401. That is deliberate: a missing credential must fail
    loudly and identically everywhere, never fall back to something that looks
    like it worked.
    """
    from settings import get_settings

    settings = get_settings()
    token = settings.coding_service_token
    return outbound_headers(
        tenant_id,
        authorization_service_token=token or None,
        app_id=settings.requirements_module_app_id or None,
    )

"""Write-time model-integrity refusals (target spec §7.3, layer 1).

A write that would create an illegal model is refused with HTTP 422 and a body
whose ``error.code`` is ``model_integrity`` and whose ``error.findings`` lists
the error-severity findings — the same objects a validation run stores — so a
caller can act on ``rule_id``/``evidence`` instead of parsing a message.
"""

from __future__ import annotations

from fastapi import HTTPException

from schemas.modeling import Finding, FindingTarget, TargetKind

MODEL_INTEGRITY = "model_integrity"


def integrity_error(findings: list[Finding]) -> HTTPException:
    errors = [f for f in findings if f.severity == "error"]
    first = errors[0] if errors else None
    message = (
        f"{first.rule_id}: {first.message}"
        + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else "")
        if first
        else "model integrity check failed"
    )
    return HTTPException(
        status_code=422,
        detail={
            "code": MODEL_INTEGRITY,
            "message": message,
            "findings": [f.model_dump(mode="json") for f in errors],
        },
    )


def single_finding(
    rule_id: str,
    ruleset_id: str,
    message: str,
    *,
    targets: list[tuple[TargetKind, str]] | None = None,
    path: str | None = None,
    evidence: dict | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        ruleset_id=ruleset_id,
        severity="error",
        targets=[FindingTarget(kind=k, id=i) for k, i in targets or []],
        path=path,
        message=message,
        evidence=evidence,
    )

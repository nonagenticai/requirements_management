"""Change sets on a UML model (target spec §9.5): the UML half of ``services/change_sets.py``.

Op kinds: ``uml_element``, ``uml_relationship``, ``interaction``, ``diagram``,
``diagram_object``, ``diagram_connection``. ``attributes`` are the route bodies of that kind
(``schemas/uml_api.py``) without the model scope; occurrences add ``diagram_id``. An update
replaces the top-level keys it names (an element's ``attributes`` or an interaction's
``lifelines`` are replaced whole, as their ``PUT`` does) and keeps the rest.

Evaluate runs the ops on the in-memory draft with the SAME operations and gate as the
routes (``services/uml_draft.py``): an op that cannot be applied is ``CS-OP-001``; an op the
write gate would refuse at apply, or a set that introduces any error-severity finding the
head does not have, makes the set ``invalid``. Apply (:func:`apply_all`) loads the draft
once, runs every op through the same in-memory operations, gates the result once and
persists it once with the route services' write core (``uml_store.commit_draft``), inside
the caller-owned transaction and under the apply lock of ``services/change_sets.py``.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import uuid7

from pydantic import BaseModel, ValidationError

from models.modeling import ModelingModel
from schemas.modeling import Finding
from schemas.modeling_lifecycle import ChangeOp
from services import uml_manifest, uml_store
from services.model_integrity import integrity_error
from services.uml_draft import (
    OCCURRENCE_KINDS,
    UML_KINDS,
    UPDATE_SCHEMAS,
    UmlDraft,
    body_fields,
    introduced_errors,
    map_refs,
    payload_dict,
)
from tools.uml.rules import validate as validate_uml

CHANGE_SET_RULESET = "modeling/change-sets"


class OpError(Exception):
    def __init__(self, findings: list[Finding]) -> None:
        super().__init__(findings[0].message if findings else "op error")
        self.findings = findings


def op_error(index: int, message: str, **evidence: Any) -> OpError:
    return OpError(
        [
            Finding(
                rule_id="CS-OP-001",
                ruleset_id=CHANGE_SET_RULESET,
                severity="error",
                targets=[],
                path=f"/ops/{index}",
                message=message,
                evidence=evidence or None,
            )
        ]
    )


def _validated(schema: type[BaseModel], data: dict[str, Any], index: int) -> BaseModel:
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise OpError(
            [
                op_error(
                    index,
                    "attributes/"
                    + "/".join(str(p) for p in err.get("loc", ()))
                    + f": {err.get('msg')}",
                ).findings[0]
                for err in exc.errors()
            ]
        ) from exc


def _apply_in_memory(w: UmlDraft, op: ChangeOp, index: int, ref: str) -> None:
    if op.kind not in UML_KINDS:
        raise op_error(index, f"{op.kind} is not a UML change kind", kind=op.kind)
    schema = UPDATE_SCHEMAS[op.kind]
    attrs = copy.deepcopy(op.attributes or {})
    occurrence = op.kind in OCCURRENCE_KINDS
    if op.op == "create":
        if w.kind_of(ref) is not None:
            raise op_error(index, f"create id {ref} is already in use", id=ref)
        diagram_id = None
        if occurrence:
            diagram_id = attrs.pop("diagram_id", None)
            if diagram_id not in w.diagrams:
                raise op_error(index, f"diagram {diagram_id} is not in this model")
        payload = _validated(schema, attrs, index)
        w.put(
            op.kind,
            payload_dict(op.kind, ref, payload, diagram_id),
            getattr(payload, "position", None),
        )
        return
    cur = w.table(op.kind).get(op.id or "")
    if cur is None:
        raise op_error(index, f"{op.kind} {op.id} is not in this model", id=op.id)
    if op.op == "delete":
        w.delete(op.kind, cur["id"])
        return
    if occurrence and attrs.pop("diagram_id", cur["diagram_id"]) != cur["diagram_id"]:
        raise op_error(index, "an occurrence cannot move to another diagram")
    payload = _validated(schema, {**body_fields(cur), **attrs}, index)
    w.put(
        op.kind,
        payload_dict(op.kind, cur["id"], payload, cur.get("diagram_id")),
        getattr(payload, "position", None),
    )


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    out = []
    for f in findings:
        key = f.model_dump_json()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def evaluate(manifest: dict[str, Any], ops: list[ChangeOp]) -> tuple[list[Finding], list[Finding]]:
    """(findings, blocking) of applying ``ops`` to a UML ``manifest`` in memory. Pure."""
    working = UmlDraft.from_manifest(manifest)
    blocking: list[Finding] = []
    step = working.snapshot()
    for index, op in enumerate(ops):
        try:
            _apply_in_memory(working, op, index, op.id or f"new:{index}")
        except OpError as exc:
            blocking.extend(exc.findings)
            continue
        # Apply runs the ops one by one through the write gate, so each op must pass it
        # on the state the ops before it left.
        nxt = working.snapshot()
        blocking.extend(introduced_errors(step, nxt))
        step = nxt
    after = working.snapshot()
    blocking.extend(
        introduced_errors(UmlDraft.from_manifest(manifest).snapshot(), after, gated_only=False)
    )
    findings = [f for f in blocking if f.rule_id == "CS-OP-001"]
    findings += validate_uml(after).findings
    return findings, _dedupe(blocking)


async def apply_all(
    session: Any,
    model: ModelingModel,
    ops: list[ChangeOp],
    id_map: dict[str, str],
    *,
    subject_id: str,
) -> None:
    """Apply a proposed set: ONE draft load, every op in memory, ONE gate, ONE persist.

    The ops run through the same in-memory operations and the same write gate and
    persistence as the routes (``uml_store.commit_draft``), with each create's local
    reference replaced by a fresh row id (``id_map``). The caller holds the model lock,
    has checked ``base_head`` and owns the transaction.
    """
    before = await uml_manifest.load_draft(session, model.id, model.tenant_id)
    working = before.copy()
    for index, op in enumerate(ops):
        if op.kind not in UML_KINDS:
            raise integrity_error(op_error(index, f"{op.kind} is not a UML change kind").findings)
        target = id_map.get(op.id or "", op.id)
        new_id = str(uuid7()) if op.op == "create" else None
        mapped = op.model_copy(
            update={
                "id": target,
                "attributes": map_refs(op.kind, op.attributes or {}, id_map),
            }
        )
        try:
            _apply_in_memory(working, mapped, index, new_id or "")
        except OpError as exc:
            raise integrity_error(exc.findings) from exc
        if new_id is not None and op.id:
            id_map[op.id] = new_id
    await uml_store.commit_draft(session, model, before, working, subject_id=subject_id)

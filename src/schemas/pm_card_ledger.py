"""Card ledger schemas (R350 (1); ``/pm/cards/{planka_card_id}/ledger``, migration 2026_11_04_0080).

A card's flows append what they learned as ledger rows: a ``fact`` as-is, a ``claim`` with
laya's verdict (``laya_score`` / ``accepted`` / ``reason``) once it has one. Append-only.
"""

import json
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

LedgerKind = Literal["fact", "claim"]
MAX_ENTRIES = 50
MAX_VALUE_BYTES = 64 * 1024  # serialized JSON, per entry


def _has_nul(obj: object) -> bool:
    """A NUL anywhere in a string (or object key): Postgres text and jsonb reject it."""
    if isinstance(obj, str):
        return "\x00" in obj
    if isinstance(obj, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_nul(v) for v in obj)
    return False


class LedgerSource(BaseModel):
    """Where an entry came from: the board list, the n8n node, the tool, the execution."""

    model_config = ConfigDict(extra="forbid")

    list: str = Field(..., min_length=1, max_length=200, description="Board list (stage)")
    node: str | None = Field(None, max_length=200, description="n8n node name")
    tool: str | None = Field(None, max_length=200, description="Tool that produced it")
    execution_id: str = Field(..., min_length=1, max_length=64, description="n8n execution id")


class LedgerEntryIn(BaseModel):
    """One fact or claim to append. Laya fields are a claim's only (a fact with them is 422)."""

    model_config = ConfigDict(extra="forbid")

    kind: LedgerKind = Field(..., description="``fact`` or ``claim``")
    key: str = Field(..., min_length=1, max_length=200, description="What the entry is about")
    value: JsonValue = Field(..., description="The fact or claim (any JSON)")
    source: LedgerSource
    laya_score: float | None = Field(
        None, allow_inf_nan=False, description="laya's score (claims only; finite)"
    )
    accepted: bool | None = Field(None, description="laya's verdict (claims only)")
    reason: str | None = Field(None, max_length=2000, description="laya's reason (claims only)")

    @field_validator("value")
    @classmethod
    def _value_is_bounded(cls, value: JsonValue) -> JsonValue:
        try:  # UTF-8 bytes; NaN/Infinity are not JSON (jsonb rejects them) — 422, not 500
            text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except ValueError:
            raise ValueError("value must be JSON: NaN and Infinity are not allowed") from None
        size = len(text.encode())
        if size > MAX_VALUE_BYTES:
            raise ValueError(f"value is {size} bytes of JSON; the limit is {MAX_VALUE_BYTES}")
        return value

    @model_validator(mode="after")
    def _no_nul_bytes(self) -> Self:
        if _has_nul([self.key, self.reason, self.source.model_dump(), self.value]):
            raise ValueError("key, reason, source and value must not contain NUL (\\u0000)")
        return self

    @model_validator(mode="after")
    def _laya_fields_are_claim_only(self) -> Self:
        laya = (self.laya_score, self.accepted, self.reason)
        if self.kind == "fact" and any(v is not None for v in laya):
            raise ValueError("laya_score, accepted and reason are only allowed on a claim")
        return self


class LedgerAppendIn(BaseModel):
    """1..50 entries for one card, appended in order in one transaction."""

    model_config = ConfigDict(extra="forbid")

    entries: list[LedgerEntryIn] = Field(..., min_length=1, max_length=MAX_ENTRIES)


class LedgerEntryOut(BaseModel):
    """A stored ledger entry."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    planka_card_id: str
    kind: LedgerKind
    key: str
    value: JsonValue
    source: LedgerSource
    laya_score: float | None = None
    accepted: bool | None = None
    reason: str | None = None
    created_at: datetime

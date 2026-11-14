"""``idempotency_keys`` — the ``Idempotency-Key`` request ledger (target spec A12).

One row per ``(tenant_id, subject_id, route, key)`` — a key is the CALLER's, never shared
with another subject of the same tenant. A create route claims the row in the SAME
transaction as its write (``status_code IS NULL`` = in flight) and then stores the
response it returned, so a retry with the same key and body is answered from the row
instead of writing again. Rows older than 24h are treated as absent
(``services/idempotency.py``). Migration ``2026_10_27_0068``; RLS ``tenant_isolation``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base

KEY_MAX_LENGTH = 255


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    tenant_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    # Resolved caller (``get_subject_id``; "" = tenant-only caller). NOT NULL so the unique
    # index treats "" like any other subject (NULLs would never collide).
    subject_id: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    route: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    resource_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "uq_idempotency_keys_tenant_subject_route_key",
            "tenant_id",
            "subject_id",
            "route",
            "key",
            unique=True,
        ),
        Index("ix_idempotency_keys_tenant_created_at", "tenant_id", "created_at"),
        CheckConstraint(
            f"char_length(key) BETWEEN 1 AND {KEY_MAX_LENGTH}", name="ck_idempotency_keys_key_len"
        ),
    )

"""EntityVersionHistory model — tracks version snapshots for all entity types."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import DateTime, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class EntityVersionHistory(Base):
    __tablename__ = "entity_version_history"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7, index=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    version_status: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_version_history_entity", "entity_type", "entity_id"),
        Index("ix_version_history_entity_version", "entity_type", "entity_id", "version"),
    )

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shelfwise_twin import TwinObservation


class EdgeObservationBatch(BaseModel):
    """Bounded batch of derived observations emitted by one registered edge device."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=8, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=100)
    store_id: str = Field(min_length=1, max_length=100)
    device_id: str = Field(min_length=8, max_length=160)
    sent_at: datetime
    observations: list[TwinObservation] = Field(min_length=1, max_length=100)

    @field_validator("sent_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        """Reject ambiguous device timestamps at the edge trust boundary."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sent_at must include a timezone")
        return value

    @field_validator("observations")
    @classmethod
    def reject_scope_mismatch(cls, value: list[TwinObservation]) -> list[TwinObservation]:
        """Require a single batch to carry one store's derived facts only."""
        stores = {item.store_id for item in value}
        if len(stores) > 1:
            raise ValueError("edge batch observations must share one store_id")
        return value


def utc_now() -> datetime:
    """Return a timezone-aware clock value for callers constructing batches."""
    return datetime.now(UTC)

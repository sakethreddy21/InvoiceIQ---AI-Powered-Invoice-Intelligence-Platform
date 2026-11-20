from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ready"]
    database: Literal["connected"]


class DashboardStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    invoices_processed: int = Field(ge=0)
    awaiting_review: int = Field(ge=0)
    compliance_rate: float = Field(ge=0, le=100)
    cross_border_flagged: int | None = Field(default=None, ge=0)
    data_mode: Literal["live"]
    by_compliance: dict[str, int] = Field(default_factory=dict)
    by_direction: dict[str, int] = Field(default_factory=dict)
    duplicates_flagged: int = 0
    processing: int = 0
    bookings_pending_approval: int = 0
    bookings_ready_to_export: int = 0
    total_gross_eur: Decimal | None = None

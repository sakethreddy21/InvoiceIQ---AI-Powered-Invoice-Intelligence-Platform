import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["admin", "accountant", "viewer"]
FactValue = Literal["goods", "services", "yes", "no"]


def _email(value: str) -> str:
    value = value.strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) or len(value) > 320:
        raise ValueError("Enter a valid email address")
    return value


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=200)

    _normalise = field_validator("email")(_email)


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=10, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    organization_name: str = Field(min_length=1, max_length=200)

    _normalise = field_validator("email")(_email)


class UserResponse(BaseModel):
    id: str
    email: str
    name: str


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    chart_of_accounts: str
    account_length: int
    datev_consultant_number: str | None
    datev_client_number: str | None
    fiscal_year_start: str
    own_identifiers: list[str]
    retention_days: int


class MembershipSummary(BaseModel):
    organization_id: str
    organization_name: str
    role: Role


class SessionResponse(BaseModel):
    user: UserResponse
    organization: OrganizationResponse
    role: Role
    memberships: list[MembershipSummary]


class TokenResponse(SessionResponse):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class OrganizationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    chart_of_accounts: Literal["SKR03", "SKR04", "custom"] | None = None
    account_length: int | None = Field(default=None, ge=4, le=8)
    datev_consultant_number: str | None = Field(default=None, pattern=r"^\d{4,7}$")
    datev_client_number: str | None = Field(default=None, pattern=r"^\d{1,5}$")
    fiscal_year_start: str | None = Field(default=None, pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
    own_identifiers: list[str] | None = Field(default=None, max_length=20)
    retention_days: int | None = Field(default=None, ge=30, le=36500)


class MemberCreate(BaseModel):
    email: str
    name: str = Field(min_length=1, max_length=200)
    role: Role
    password: str | None = Field(default=None, min_length=10, max_length=200)

    _normalise = field_validator("email")(_email)


class MemberUpdate(BaseModel):
    role: Role


class MemberResponse(BaseModel):
    user_id: str
    email: str
    name: str
    role: Role


class MappingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    keywords: list[str] = Field(min_length=1, max_length=30)
    expense_account: str = Field(pattern=r"^\d{4,9}$")
    creditor_account: str = Field(pattern=r"^\d{4,9}$")
    tax_key: str | None = Field(default=None, pattern=r"^\d{1,4}$")
    direction: Literal["incoming", "outgoing"] = "incoming"
    tax_treatment: str | None = Field(default=None, max_length=200)
    priority: int = Field(default=100, ge=0, le=10_000)
    active: bool = True

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, keywords: list[str]) -> list[str]:
        cleaned = [keyword.strip()[:80] for keyword in keywords if keyword.strip()]
        if not cleaned:
            raise ValueError("Add at least one keyword")
        return cleaned


class MappingResponse(MappingInput):
    model_config = ConfigDict(from_attributes=True)

    id: str
    is_demo: bool = False


class ChartAccountInput(BaseModel):
    number: str = Field(pattern=r"^\d{4,9}$")
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["expense", "revenue", "creditor", "debtor", "other"]


class ChartAccountResponse(ChartAccountInput):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chart: str
    is_demo: bool


class SuggestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_id: str
    invoice_filename: str
    mapping_id: str | None
    status: Literal["proposed", "approved", "rejected", "exported"]
    booking: dict[str, object]
    explanation: str
    created_by: str
    decided_by: str | None
    decided_at: datetime | None
    export_batch_id: str | None
    created_at: datetime


class ExportPreviewRow(BaseModel):
    suggestion: SuggestionResponse
    errors: list[str]


class ExportPreview(BaseModel):
    organization_errors: list[str]
    rows: list[ExportPreviewRow]
    exportable: int
    disclaimer: str


class ExportBatchResponse(BaseModel):
    id: str
    filename: str
    booking_count: int
    created_by: str
    created_at: datetime


YesNo = Literal["yes", "no"]


class FactsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supply_type: Literal["goods", "services"] | None = None
    supplier_is_business: YesNo | None = None
    customer_is_business: YesNo | None = None
    supplier_registered: YesNo | None = None
    customer_registered: YesNo | None = None
    place_of_supply: Literal["germany", "india", "other"] | None = None
    goods_physically_moved: YesNo | None = None
    payment_in_convertible_foreign_exchange: YesNo | None = None
    distinct_persons: YesNo | None = None
    export_evidence_available: YesNo | None = None
    electronically_supplied: YesNo | None = None


class AdvisorReview(BaseModel):
    status: Literal["pending_review", "reviewed", "needs_more_information"]
    note: str | None = Field(default=None, max_length=2000)


class FxRequest(BaseModel):
    provider: Literal["manual", "ecb", "demo"]
    units_per_eur: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    rate_date: date | None = None
    source: str | None = Field(default=None, max_length=300)

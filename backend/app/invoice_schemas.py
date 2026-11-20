from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str | None = Field(default=None, max_length=1000)
    quantity: str | None = Field(default=None, max_length=30)
    unit_price: str | None = Field(default=None, max_length=30)
    net_amount: str | None = Field(default=None, max_length=30)
    vat_rate: str | None = Field(default=None, max_length=10)
    vat_amount: str | None = Field(default=None, max_length=30)


class InvoiceData(BaseModel):
    """Editable invoice fields. All values are strings as printed/normalised; amounts are decimal strings."""

    model_config = ConfigDict(extra="forbid", str_max_length=2000)

    supplier_name: str | None = None
    supplier_address: str | None = None
    supplier_tax_id: str | None = None
    supplier_tax_number: str | None = None
    supplier_vat_id: str | None = None
    supplier_gstin: str | None = None
    supplier_country: str | None = None
    customer_name: str | None = None
    customer_address: str | None = None
    customer_tax_id: str | None = None
    customer_tax_number: str | None = None
    customer_vat_id: str | None = None
    customer_gstin: str | None = None
    customer_country: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    service_date: str | None = None
    reference_invoice_number: str | None = None
    currency: str | None = None
    place_of_supply: str | None = None
    tax_type: str | None = None
    cgst_rate: str | None = None
    sgst_rate: str | None = None
    igst_rate: str | None = None
    net_amount: str | None = None
    vat_rate: str | None = None
    vat_amount: str | None = None
    gross_amount: str | None = None
    description: str | None = None
    document_type: str | None = None
    reverse_charge_wording: str | None = None
    tax_exemption_reference: str | None = None
    transaction_direction: str | None = None
    advisor_review_required: str | None = None
    exchange_rate: str | None = None
    exchange_rate_date: str | None = None
    exchange_rate_source: str | None = None
    exchange_rate_status: str | None = None
    iban: str | None = None
    bic: str | None = None
    payment_terms: str | None = None
    line_items: list[LineItem] | None = Field(default=None, max_length=500)


class InvoiceResponse(BaseModel):
    id: str
    filename: str
    content_type: str
    file_size: int
    sha256: str
    status: str
    compliance_status: str
    direction: str
    source_channel: str
    extraction_method: str
    extraction_provider: str
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    currency: str | None
    gross_amount: Decimal | None
    original_data: dict[str, object]
    corrected_data: dict[str, object]
    effective_data: dict[str, object]
    field_meta: dict[str, object]
    pages: list[dict[str, object]]
    validation_results: list[dict[str, object]]
    audit_history: list[dict[str, object]]
    transaction_facts: dict[str, object]
    fx_conversion: dict[str, object] | None
    created_at: datetime
    updated_at: datetime


class UploadResult(BaseModel):
    invoice: InvoiceResponse
    duplicate: bool


class UploadResponse(BaseModel):
    files: list[UploadResult]


class InvoicePatch(BaseModel):
    fields: InvoiceData


class ValidationResponse(BaseModel):
    invoice_id: str
    results: list[dict[str, object]]
    overall_status: Literal["PASS", "FAIL", "NEEDS_REVIEW"]


class InvoiceListResponse(BaseModel):
    items: list[InvoiceResponse]
    total: int = Field(ge=0)

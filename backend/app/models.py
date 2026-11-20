from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

Money = Numeric(18, 2)
Rate = Numeric(9, 4)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    chart_of_accounts: Mapped[str] = mapped_column(String(20), default="SKR03", nullable=False)
    account_length: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    datev_consultant_number: Mapped[str | None] = mapped_column(String(7))
    datev_client_number: Mapped[str | None] = mapped_column(String(5))
    fiscal_year_start: Mapped[str] = mapped_column(String(5), default="01-01", nullable=False)
    # Names/VAT IDs of the organization itself, used to tell purchase from sales invoices.
    own_identifiers: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=3650, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id", name="uq_membership_user_org"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("organization_id", "sha256", name="uq_invoice_org_sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="needs_review", nullable=False)
    compliance_status: Mapped[str] = mapped_column(String(20), default="NOT_CHECKED", index=True, nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(100), index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, index=True)
    supplier_name: Mapped[str | None] = mapped_column(String(255), index=True)
    currency: Mapped[str | None] = mapped_column(String(3), index=True)
    gross_amount: Mapped[Decimal | None] = mapped_column(Money)
    direction: Mapped[str] = mapped_column(String(10), default="incoming", nullable=False)
    source_channel: Mapped[str] = mapped_column(String(20), default="upload", nullable=False)
    extraction_provider: Mapped[str] = mapped_column(String(30), default="rules", nullable=False)
    field_meta: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    pages: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(30), default="text", nullable=False)
    original_data: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    corrected_data: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    validation_results: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list, nullable=False)
    audit_history: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list, nullable=False)
    transaction_facts: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    fx_conversion: Mapped[dict[str, object] | None] = mapped_column(JSON)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    def effective_data(self) -> dict[str, object]:
        return {**self.original_data, **self.corrected_data}

    def add_audit(self, action: str, actor: str, **details: object) -> None:
        entry = {"action": action, "actor": actor, "timestamp": _now().isoformat(), **details}
        self.audit_history = [*self.audit_history, entry]
        pending = self.__dict__.setdefault("_pending_audit", [])
        pending.append(entry)


class AccountMapping(Base):
    """Organization-defined booking rule. InvoiceIQ ships no account numbers of its own."""

    __tablename__ = "account_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expense_account: Mapped[str] = mapped_column(String(9), nullable=False)
    creditor_account: Mapped[str] = mapped_column(String(9), nullable=False)
    tax_key: Mapped[str | None] = mapped_column(String(4))
    direction: Mapped[str] = mapped_column(String(10), default="incoming", nullable=False)
    tax_treatment: Mapped[str | None] = mapped_column(String(200))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class BookingSuggestion(Base):
    __tablename__ = "booking_suggestions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    mapping_id: Mapped[str | None] = mapped_column(ForeignKey("account_mappings.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    booking: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(320))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    export_batch_id: Mapped[str | None] = mapped_column(ForeignKey("export_batches.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ExportBatch(Base):
    __tablename__ = "export_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    booking_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class InvoiceFile(Base):
    __tablename__ = "invoice_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # original | embedded_xml
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ExtractionResult(Base):
    __tablename__ = "extraction_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    method: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    fields: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    field_meta: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class InvoiceParty(Base):
    __tablename__ = "invoice_parties"
    __table_args__ = (UniqueConstraint("invoice_id", "role", name="uq_invoice_party_role"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # supplier | customer
    name: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(80))
    vat_id: Mapped[str | None] = mapped_column(String(20), index=True)
    gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    tax_number: Mapped[str | None] = mapped_column(String(40))


class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unit_price: Mapped[Decimal | None] = mapped_column(Money)
    net_amount: Mapped[Decimal | None] = mapped_column(Money)
    vat_rate: Mapped[Decimal | None] = mapped_column(Rate)
    vat_amount: Mapped[Decimal | None] = mapped_column(Money)


class InvoiceTaxDetail(Base):
    __tablename__ = "invoice_tax_details"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    tax_type: Mapped[str] = mapped_column(String(40), nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Rate)
    taxable_amount: Mapped[Decimal | None] = mapped_column(Money)
    tax_amount: Mapped[Decimal | None] = mapped_column(Money)
    exemption_reference: Mapped[str | None] = mapped_column(Text)


class ValidationResultRecord(Base):
    __tablename__ = "validation_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    rule_id: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    result: Mapped[str] = mapped_column(String(15), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    detected_value: Mapped[str | None] = mapped_column(Text)
    expected_value: Mapped[str | None] = mapped_column(Text)
    suggested_correction: Mapped[str | None] = mapped_column(Text)
    legal_reference: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class CrossBorderAssessment(Base):
    __tablename__ = "cross_border_assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(40), nullable=False)
    transaction_type: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    advisor_status: Mapped[str] = mapped_column(String(20), default="pending_review", nullable=False)
    result: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    facts: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(320))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    advisor_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    __table_args__ = (UniqueConstraint("provider", "currency", "rate_date", name="uq_exchange_rate"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), index=True, nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    units_per_eur: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class ChartAccount(Base):
    __tablename__ = "chart_accounts"
    __table_args__ = (UniqueConstraint("organization_id", "number", name="uq_chart_account_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    chart: Mapped[str] = mapped_column(String(20), nullable=False)
    number: Mapped[str] = mapped_column(String(9), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # expense | revenue | creditor | debtor | other
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class TaxRule(Base):
    """Versioned, effective-dated cross-border rule definitions (seeded from app.cross_border)."""

    __tablename__ = "tax_rules"
    __table_args__ = (UniqueConstraint("rule_id", "version", name="uq_tax_rule_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    rule_id: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(40), nullable=False)
    supply_type: Mapped[str] = mapped_column(String(10), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    definition: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_entity", "entity_type", "entity_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True, nullable=False)


@event.listens_for(Session, "before_flush")
def _write_invoice_audit_logs(session: Session, _context, _instances) -> None:
    """Mirror Invoice.add_audit() entries into the audit_logs table."""
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Invoice):
            for entry in obj.__dict__.pop("_pending_audit", []):
                details = {k: v for k, v in entry.items() if k not in ("action", "actor", "timestamp")}
                session.add(
                    AuditLog(
                        organization_id=obj.organization_id,
                        actor=str(entry["actor"]),
                        entity_type="invoice",
                        entity_id=obj.id,
                        action=str(entry["action"]),
                        details=details,
                    )
                )

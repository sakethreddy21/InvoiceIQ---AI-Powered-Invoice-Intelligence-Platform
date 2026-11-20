"""Keeps the normalized invoice tables in sync with an invoice's effective data."""

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.accounting import invoice_direction
from app.models import (
    AuditLog,
    Invoice,
    InvoiceLineItem,
    InvoiceParty,
    InvoiceTaxDetail,
    Organization,
    ValidationResultRecord,
)


def _decimal(value: object, places: str | None = None) -> Decimal | None:
    try:
        if value in (None, ""):
            return None
        amount = Decimal(str(value))
        if not amount.is_finite():
            return None
        return amount.quantize(Decimal(places)) if places else amount
    except (InvalidOperation, ValueError):
        return None


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def compliance_status(results: list[dict[str, object]]) -> str:
    if not results:
        return "NOT_CHECKED"
    statuses = {str(r.get("result")) for r in results}
    return "FAIL" if "FAIL" in statuses else "NEEDS_REVIEW" if "NEEDS_REVIEW" in statuses else "PASS"


def sync_invoice(db: Session, invoice: Invoice, organization: Organization) -> None:
    data = invoice.effective_data()
    invoice.invoice_number = (str(data.get("invoice_number") or "").strip() or None) and str(data["invoice_number"])[:100]
    invoice.invoice_date = _date(data.get("invoice_date"))
    invoice.supplier_name = (str(data.get("supplier_name") or "").strip() or None) and str(data["supplier_name"])[:255]
    invoice.currency = (str(data.get("currency") or "").upper()[:3] or None)
    invoice.gross_amount = _decimal(data.get("gross_amount"), "0.01")
    invoice.compliance_status = compliance_status(invoice.validation_results)
    invoice.direction = invoice_direction(data, organization)

    db.execute(delete(InvoiceParty).where(InvoiceParty.invoice_id == invoice.id))
    db.execute(delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice.id))
    db.execute(delete(InvoiceTaxDetail).where(InvoiceTaxDetail.invoice_id == invoice.id))
    for role in ("supplier", "customer"):
        if any(data.get(f"{role}_{key}") for key in ("name", "address", "vat_id", "gstin")):
            db.add(InvoiceParty(
                invoice_id=invoice.id, role=role,
                name=_str(data.get(f"{role}_name"), 255), address=_str(data.get(f"{role}_address")),
                country=_str(data.get(f"{role}_country"), 80), vat_id=_str(data.get(f"{role}_vat_id"), 20),
                gstin=_str(data.get(f"{role}_gstin"), 15), tax_number=_str(data.get(f"{role}_tax_number"), 40),
            ))
    for position, item in enumerate(data.get("line_items") or [], start=1):
        if isinstance(item, dict):
            db.add(InvoiceLineItem(
                invoice_id=invoice.id, position=position, description=_str(item.get("description")),
                quantity=_decimal(item.get("quantity")), unit_price=_decimal(item.get("unit_price"), "0.01"),
                net_amount=_decimal(item.get("net_amount"), "0.01"), vat_rate=_decimal(item.get("vat_rate")),
                vat_amount=_decimal(item.get("vat_amount"), "0.01"),
            ))
    for detail in tax_details(data):
        db.add(InvoiceTaxDetail(invoice_id=invoice.id, **detail))


def tax_details(data: dict[str, object]) -> list[dict[str, object]]:
    explicit = data.get("tax_details")
    exemption = _str(data.get("tax_exemption_reference"))
    if isinstance(explicit, list) and explicit:
        return [
            {"tax_type": str(d.get("tax_type") or "VAT"), "rate": _decimal(d.get("rate")),
             "taxable_amount": _decimal(d.get("taxable_amount"), "0.01"), "tax_amount": _decimal(d.get("tax_amount"), "0.01"),
             "exemption_reference": _str(d.get("exemption_reason")) or exemption}
            for d in explicit if isinstance(d, dict)
        ]
    net, vat = _decimal(data.get("net_amount"), "0.01"), _decimal(data.get("vat_amount"), "0.01")
    if net is None and vat is None:
        return []
    tax_type = str(data.get("tax_type") or "VAT")
    if tax_type == "CGST + SGST":
        half = (vat / 2).quantize(Decimal("0.01")) if vat is not None else None
        return [
            {"tax_type": "CGST", "rate": _decimal(data.get("cgst_rate")), "taxable_amount": net, "tax_amount": half, "exemption_reference": None},
            {"tax_type": "SGST", "rate": _decimal(data.get("sgst_rate")), "taxable_amount": net, "tax_amount": (vat - half) if vat is not None and half is not None else None, "exemption_reference": None},
        ]
    return [{"tax_type": tax_type[:40], "rate": _decimal(data.get("vat_rate")), "taxable_amount": net, "tax_amount": vat, "exemption_reference": exemption}]


def store_validation_results(db: Session, invoice: Invoice, results: list[dict[str, object]]) -> None:
    db.execute(delete(ValidationResultRecord).where(ValidationResultRecord.invoice_id == invoice.id))
    for result in results:
        db.add(ValidationResultRecord(
            organization_id=invoice.organization_id, invoice_id=invoice.id, rule_id=str(result["rule_id"]),
            result=str(result["result"]), severity=str(result["severity"]), explanation=str(result["explanation"]),
            detected_value=_str(result.get("detected_value")), expected_value=_str(result.get("expected_value")),
            suggested_correction=_str(result.get("suggested_correction")), legal_reference=str(result["legal_reference"]),
        ))


def find_duplicates(db: Session, invoice: Invoice) -> list[dict[str, object]]:
    """Other invoices of the organization with the same file, or the same supplier + invoice number."""
    data = invoice.effective_data()
    number = str(data.get("invoice_number") or "").strip()
    conditions = [Invoice.sha256 == invoice.sha256]
    if number:
        conditions.append(Invoice.invoice_number == number)
    candidates = db.scalars(
        select(Invoice).where(Invoice.organization_id == invoice.organization_id, Invoice.id != invoice.id, or_(*conditions))
    ).all()
    supplier_keys = {str(data.get(k) or "").strip().casefold() for k in ("supplier_name", "supplier_vat_id", "supplier_gstin")} - {""}
    duplicates = []
    for other in candidates:
        other_data = other.effective_data()
        if other.sha256 == invoice.sha256:
            duplicates.append({"id": other.id, "filename": other.filename, "reason": "identical file"})
            continue
        other_keys = {str(other_data.get(k) or "").strip().casefold() for k in ("supplier_name", "supplier_vat_id", "supplier_gstin")} - {""}
        if number and str(other_data.get("invoice_number") or "").strip() == number and (supplier_keys & other_keys or not supplier_keys):
            duplicates.append({"id": other.id, "filename": other.filename, "reason": f"same supplier and invoice number {number}"})
    return duplicates


def record_audit(db: Session, organization_id: str | None, actor: str, entity_type: str, entity_id: str | None, action: str, **details: object) -> None:
    db.add(AuditLog(organization_id=organization_id, actor=actor, entity_type=entity_type, entity_id=entity_id, action=action, details=details))


def _str(value: object, limit: int | None = None) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return (text[:limit] if limit else text) or None

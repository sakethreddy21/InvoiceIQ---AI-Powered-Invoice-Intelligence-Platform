"""Creates a separate, clearly labelled demo workspace with the six fictional test invoices."""

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import cross_border, fx
from app.auth import Context, admin, get_context
from app.db import get_db
from app.demo_data import DEMO_CHART, DEMO_MAPPINGS, SELF_NAME, SELF_VAT, demo_files
from app.ingest import create_invoice, run_checks
from app.models import (
    AccountMapping, AuditLog, BookingSuggestion, ChartAccount, CrossBorderAssessment, ExportBatch, Invoice, InvoiceFile,
    Membership, Organization,
)
from app.records import record_audit
from app.settings import settings

router = APIRouter(prefix="/api/demo", tags=["demo"])
DEMO_ORG_NAME = f"{SELF_NAME} — DEMO (fictional)"


@router.get("/files")
def list_demo_files(_context: Context = Depends(get_context)) -> list[dict[str, str]]:
    return [{"filename": name, "description": description} for name, _, description in demo_files()]


DEMO_MARKER = "demo_workspace_created"


def _is_demo_organization(db: Session, organization_id: str) -> bool:
    """Demo workspaces are recognised by their creation audit entry, so renaming one does not break reset."""
    return db.scalar(select(AuditLog.id).where(
        AuditLog.organization_id == organization_id, AuditLog.entity_type == "organization", AuditLog.action == DEMO_MARKER,
    ).limit(1)) is not None


def _existing_demo_organization(db: Session, context: Context) -> Organization | None:
    memberships = db.scalars(select(Membership).where(Membership.user_id == context.user.id, Membership.role == "admin")).all()
    for membership in memberships:
        if _is_demo_organization(db, membership.organization_id):
            return db.get(Organization, membership.organization_id)
    return None


def _clear_demo_data(db: Session, organization: Organization) -> None:
    """Remove every invoice, booking, export, assessment and demo mapping so the workspace starts fresh."""
    invoice_ids = list(db.scalars(select(Invoice.id).where(Invoice.organization_id == organization.id)))
    paths = [Path(p) for p in db.scalars(select(InvoiceFile.storage_path).where(InvoiceFile.organization_id == organization.id))]
    paths += [Path(p) for p in db.scalars(select(Invoice.file_path).where(Invoice.organization_id == organization.id))]
    for model in (BookingSuggestion, CrossBorderAssessment, InvoiceFile):
        db.execute(delete(model).where(model.organization_id == organization.id))
    for invoice in db.scalars(select(Invoice).where(Invoice.organization_id == organization.id)):
        db.delete(invoice)
    db.flush()
    for model in (ExportBatch, AccountMapping, ChartAccount):
        db.execute(delete(model).where(model.organization_id == organization.id))
    db.execute(delete(AuditLog).where(AuditLog.organization_id == organization.id, AuditLog.entity_type == "invoice",
                                      AuditLog.entity_id.in_(invoice_ids)))
    db.flush()
    storage = Path(settings.upload_dir).resolve()
    for path in paths:
        if path.resolve().parent == storage:
            path.unlink(missing_ok=True)


def _seed_demo(db: Session, organization: Organization, context: Context) -> list[dict[str, object]]:
    for number, name, kind in DEMO_CHART:
        db.add(ChartAccount(organization_id=organization.id, chart="SKR03", number=number, name=name, kind=kind, is_demo=True))
    for mapping in DEMO_MAPPINGS:
        db.add(AccountMapping(organization_id=organization.id, is_demo=True, **mapping))
    db.flush()
    results: list[dict[str, object]] = []
    for filename, content, description in demo_files():
        invoice, duplicate, _ = create_invoice(db, organization, context.actor, filename, content, channel="demo")
        values = invoice.effective_data()
        # Record facts a person would confirm for the cross-border cases, so the demo shows full assessments.
        direction = cross_border.classify(values)
        if direction in ("India_to_Germany", "Germany_to_India"):
            invoice.transaction_facts = {
                "supply_type": "services", "supplier_is_business": "yes", "customer_is_business": "yes",
                "supplier_registered": "yes", "customer_registered": "yes",
                **({"payment_in_convertible_foreign_exchange": "yes", "distinct_persons": "no", "place_of_supply": "germany"} if direction == "India_to_Germany" else {"place_of_supply": "india"}),
            }
            invoice.add_audit("facts_updated", context.actor, facts=invoice.transaction_facts, note="demo data")
        conversion_note = None
        if values.get("currency") not in (None, "EUR"):
            rate_date = date.fromisoformat(str(values.get("invoice_date")))
            try:
                rate = fx.ecb_rate(str(values["currency"]), rate_date)
            except fx.FxError:
                rate = fx.demo_rate(str(values["currency"]), rate_date)
            fx.store_rate(db, rate)
            invoice.fx_conversion = fx.convert(values, rate)
            invoice.corrected_data = {
                **invoice.corrected_data, "exchange_rate": str(rate.units_per_eur), "exchange_rate_date": rate.rate_date.isoformat(),
                "exchange_rate_source": rate.source, "exchange_rate_status": "provided",
            }
            invoice.add_audit("currency_converted", context.actor, provider=rate.provider, rate=str(rate.units_per_eur))
            run_checks(db, invoice, organization, context.actor)
            conversion_note = f"{rate.provider} rate {rate.units_per_eur} ({rate.rate_date})"
        results.append({"filename": filename, "description": description, "invoice_id": invoice.id,
                        "compliance_status": invoice.compliance_status, "duplicate_file": duplicate, "fx": conversion_note})
    return results


@router.post("/workspace", status_code=201)
def create_demo_workspace(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, object]:
    """Creates the demo workspace, or resets the caller's existing one so repeated demos do not pile up workspaces."""
    organization = _existing_demo_organization(db, context)
    reused = organization is not None
    if organization is None:
        organization = Organization(
            name=DEMO_ORG_NAME, chart_of_accounts="SKR03", account_length=4, datev_consultant_number="1001",
            datev_client_number="1", fiscal_year_start="01-01", own_identifiers=[SELF_NAME, SELF_VAT], retention_days=3650,
        )
        db.add(organization)
        db.flush()
        db.add(Membership(user_id=context.user.id, organization_id=organization.id, role="admin"))
        record_audit(db, organization.id, context.actor, "organization", organization.id, DEMO_MARKER)
    else:
        _clear_demo_data(db, organization)
        record_audit(db, organization.id, context.actor, "organization", organization.id, "demo_workspace_reset")
    results = _seed_demo(db, organization, context)
    db.commit()
    return {"organization_id": organization.id, "organization_name": organization.name, "reused": reused, "invoices": results}


@router.get("/workspace")
def demo_workspace_status(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, bool]:
    return {"is_demo": _is_demo_organization(db, context.organization.id)}


@router.post("/workspace/reset")
def reset_demo_workspace(context: Context = Depends(admin), db: Session = Depends(get_db)) -> dict[str, object]:
    """Restores the currently selected demo workspace to its initial six invoices. Refuses on real workspaces."""
    organization = context.organization
    if not _is_demo_organization(db, organization.id):
        raise HTTPException(status_code=400, detail="Only a demo workspace can be reset. Your real workspaces are never touched.")
    _clear_demo_data(db, organization)
    record_audit(db, organization.id, context.actor, "organization", organization.id, "demo_workspace_reset")
    results = _seed_demo(db, organization, context)
    db.commit()
    return {"organization_id": organization.id, "organization_name": organization.name, "reused": True, "invoices": results}

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pymupdf
from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from app import cross_border, fx
from app.api_schemas import AdvisorReview, FactsUpdate, FxRequest
from app.auth import Context, editor, get_context
from app.db import get_db
from app.extraction import extract_document, ocr_available
from app.ingest import apply_extraction, create_invoice, process_in_background, recheck_duplicates_after_delete, run_checks
from app.invoice_schemas import (
    InvoiceListResponse,
    InvoicePatch,
    InvoiceResponse,
    UploadResponse,
    UploadResult,
    ValidationResponse,
)
from app.llm import llm_enabled
from app.models import AuditLog, BookingSuggestion, CrossBorderAssessment, Invoice, InvoiceFile, TaxRule
from app.ocr import available_provider
from app.records import compliance_status
from app.schemas import DashboardStats
from app.security import create_token, read_token
from app.settings import settings
from app.tax_ids import gstin_details, vat_id_details, verify_vat_registration

router = APIRouter(prefix="/api", tags=["invoices"])
MAX_FILES_PER_UPLOAD = 20
MAX_BATCH_BYTES = 50 * 1024 * 1024


def invoice_response(invoice: Invoice) -> InvoiceResponse:
    return InvoiceResponse(
        id=invoice.id, filename=invoice.filename, content_type=invoice.content_type, file_size=invoice.file_size,
        sha256=invoice.sha256, status=invoice.status, compliance_status=invoice.compliance_status, direction=invoice.direction,
        source_channel=invoice.source_channel, extraction_method=invoice.extraction_method,
        extraction_provider=invoice.extraction_provider, invoice_number=invoice.invoice_number, invoice_date=invoice.invoice_date,
        supplier_name=invoice.supplier_name, currency=invoice.currency, gross_amount=invoice.gross_amount,
        original_data=invoice.original_data, corrected_data=invoice.corrected_data, effective_data=invoice.effective_data(),
        field_meta=invoice.field_meta or {}, pages=invoice.pages or [], validation_results=invoice.validation_results,
        audit_history=invoice.audit_history, transaction_facts=invoice.transaction_facts or {},
        fx_conversion=invoice.fx_conversion, created_at=invoice.created_at, updated_at=invoice.updated_at,
    )


def get_scoped_invoice(db: Session, context: Context, invoice_id: str) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    # Invoices of other organizations are reported as missing, never as forbidden.
    if not invoice or invoice.organization_id != context.organization.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def _discard_pending_booking(db: Session, invoice: Invoice, actor: str) -> None:
    suggestion = db.scalar(select(BookingSuggestion).where(BookingSuggestion.invoice_id == invoice.id))
    if suggestion and suggestion.status != "exported":
        db.delete(suggestion)
        invoice.add_audit("booking_discarded", actor, reason="invoice data changed", previous_status=suggestion.status)
        if invoice.status == "approved":
            invoice.status = "needs_review"


@router.get("/dashboard/stats", response_model=DashboardStats, tags=["dashboard"])
def dashboard_stats(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> DashboardStats:
    invoices = db.scalars(
        select(Invoice).options(defer(Invoice.extracted_text)).where(Invoice.organization_id == context.organization.id)
    ).all()
    by_compliance: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    total_eur = Decimal("0")
    duplicates = 0
    for invoice in invoices:
        by_compliance[invoice.compliance_status] = by_compliance.get(invoice.compliance_status, 0) + 1
        direction = cross_border.classify(invoice.effective_data())
        by_direction[direction] = by_direction.get(direction, 0) + 1
        duplicates += any(r.get("rule_id") == "DUPLICATE-INVOICE" and r.get("result") == "FAIL" for r in invoice.validation_results)
        if invoice.gross_amount is not None and invoice.currency == "EUR":
            total_eur += invoice.gross_amount
        elif invoice.fx_conversion and (invoice.fx_conversion.get("converted") or {}).get("gross_amount"):
            total_eur += Decimal(str(invoice.fx_conversion["converted"]["gross_amount"]))
    suggestions = db.scalars(select(BookingSuggestion.status).where(BookingSuggestion.organization_id == context.organization.id)).all()
    count = len(invoices)
    return DashboardStats(
        invoices_processed=count,
        awaiting_review=sum(i.status in ("needs_review", "validation_failed", "processing") for i in invoices),
        compliance_rate=round(100 * by_compliance.get("PASS", 0) / count, 1) if count else 0,
        cross_border_flagged=sum(v for k, v in by_direction.items() if k in ("India_to_Germany", "Germany_to_India", "other_international")),
        data_mode="live",
        by_compliance=by_compliance,
        by_direction=by_direction,
        duplicates_flagged=duplicates,
        processing=sum(i.status == "processing" for i in invoices),
        bookings_pending_approval=sum(s == "proposed" for s in suggestions),
        bookings_ready_to_export=sum(s == "approved" for s in suggestions),
        total_gross_eur=total_eur.quantize(Decimal("0.01")) if count else None,
    )


@router.get("/capabilities", tags=["system"])
def capabilities(_context: Context = Depends(get_context)) -> dict[str, object]:
    return {
        "ocr": ocr_available(),
        "ocr_provider": available_provider(),
        "llm_extraction": llm_enabled(),
        "llm_provider": settings.llm_provider if llm_enabled() else None,
        "vies_verification": settings.vies_enabled,
        "fx_providers": ["ecb", "manual", "demo"],
        "email_ingestion": False,
    }


@router.get("/ingestion/email", tags=["ingestion"])
def email_ingestion(context: Context = Depends(get_context)) -> dict[str, object]:
    """Integration-ready interface. No mailbox is connected, so nothing is received by email."""
    return {
        "status": "not_configured",
        "functional": False,
        "forwarding_address": None,
        "proposed_address": f"invoices+{context.organization.id[:8]}@<your-inbound-domain>",
        "pipeline": "Received attachments would be passed to the same ingestion pipeline as uploads (source_channel='email').",
        "requirements": ["An inbound email provider webhook (e.g. SES, Postmark, SendGrid)", "SPF/DKIM checks and an allow-list of senders"],
    }


@router.get("/invoices", response_model=InvoiceListResponse)
def list_invoices(
    search: str | None = Query(default=None, max_length=100),
    date_from: date | None = None,
    date_to: date | None = None,
    currency: str | None = Query(default=None, pattern=r"^[A-Z]{3}$"),
    compliance: str | None = Query(default=None, pattern=r"^(PASS|FAIL|NEEDS_REVIEW|NOT_CHECKED)$"),
    status: str | None = Query(default=None, max_length=30),
    direction: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: Context = Depends(get_context),
    db: Session = Depends(get_db),
) -> InvoiceListResponse:
    query = select(Invoice).options(defer(Invoice.extracted_text)).where(Invoice.organization_id == context.organization.id)
    if search:
        term = f"%{search.strip()}%"
        query = query.where(or_(Invoice.filename.ilike(term), Invoice.supplier_name.ilike(term), Invoice.invoice_number.ilike(term)))
    if date_from:
        query = query.where(Invoice.invoice_date >= date_from)
    if date_to:
        query = query.where(Invoice.invoice_date <= date_to)
    if currency:
        query = query.where(Invoice.currency == currency)
    if compliance:
        query = query.where(Invoice.compliance_status == compliance)
    if status:
        query = query.where(Invoice.status == status)
    invoices = db.scalars(query.order_by(Invoice.created_at.desc())).all()
    if direction:
        invoices = [invoice for invoice in invoices if cross_border.classify(invoice.effective_data()) == direction]
    page = invoices[offset : offset + limit]
    return InvoiceListResponse(items=[invoice_response(invoice) for invoice in page], total=len(invoices))


@router.post("/invoices/upload", response_model=UploadResponse, status_code=201)
async def upload_invoices(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    context: Context = Depends(editor),
    db: Session = Depends(get_db),
) -> UploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="Select at least one invoice file")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(status_code=413, detail=f"Upload no more than {MAX_FILES_PER_UPLOAD} files at a time")
    staged: list[tuple[str, bytes]] = []
    total = 0
    for uploaded in files:
        content = await uploaded.read(settings.max_upload_bytes + 1)
        total += len(content)
        if total > MAX_BATCH_BYTES:
            raise HTTPException(status_code=413, detail="A multi-file upload cannot exceed 50 MB in total")
        staged.append((uploaded.filename or "", content))
    results: list[UploadResult] = []
    created: list[Invoice] = []
    queued: list[str] = []
    try:
        for filename, content in staged:
            invoice, duplicate, needs_background = create_invoice(db, context.organization, context.actor, filename, content)
            if not duplicate:
                created.append(invoice)
            if needs_background:
                queued.append(invoice.id)
            results.append(UploadResult(invoice=invoice_response(invoice), duplicate=duplicate))
        db.commit()
    except (HTTPException, IntegrityError) as exc:
        db.rollback()
        for invoice in created:
            for path in Path(invoice.file_path).parent.glob(f"{invoice.id}*"):
                path.unlink(missing_ok=True)
        if isinstance(exc, IntegrityError):
            raise HTTPException(status_code=409, detail="The same file was uploaded concurrently; refresh and try again") from exc
        raise
    for invoice_id in queued:
        background.add_task(process_in_background, invoice_id, context.actor)
    return UploadResponse(files=results)


@router.get("/invoices/{invoice_id}", response_model=InvoiceResponse)
def get_invoice(invoice_id: str, context: Context = Depends(get_context), db: Session = Depends(get_db)) -> InvoiceResponse:
    return invoice_response(get_scoped_invoice(db, context, invoice_id))


@router.get("/invoices/{invoice_id}/audit", tags=["audit"])
def invoice_audit(invoice_id: str, context: Context = Depends(get_context), db: Session = Depends(get_db)) -> list[dict[str, object]]:
    invoice = get_scoped_invoice(db, context, invoice_id)
    rows = db.scalars(
        select(AuditLog).where(AuditLog.entity_type == "invoice", AuditLog.entity_id == invoice.id).order_by(AuditLog.created_at)
    ).all()
    return [{"action": r.action, "actor": r.actor, "timestamp": r.created_at.isoformat(), "details": r.details} for r in rows]


@router.get("/invoices/{invoice_id}/file-url")
def get_invoice_file_url(invoice_id: str, context: Context = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, object]:
    invoice = get_scoped_invoice(db, context, invoice_id)
    token = create_token("file", invoice.id, settings.file_token_ttl_seconds, org=invoice.organization_id)
    return {
        "url": f"/api/invoices/{invoice.id}/file?token={token}",
        "page_url_template": f"/api/invoices/{invoice.id}/pages/{{page}}?token={token}",
        "token": token,
    }


def _file_invoice(db: Session, invoice_id: str, token: str | None, authorization: str | None) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    allowed = False
    if invoice and token:
        payload = read_token(token, "file")
        allowed = bool(payload and payload.get("sub") == invoice.id and payload.get("org") == invoice.organization_id)
    elif invoice and authorization and authorization.lower().startswith("bearer "):
        payload = read_token(authorization[7:].strip(), "access")
        if payload:
            from app.models import Membership

            allowed = bool(db.scalar(select(Membership).where(Membership.user_id == str(payload["sub"]), Membership.organization_id == invoice.organization_id)))
    if not invoice or not allowed:
        raise HTTPException(status_code=404, detail="Invoice file not found or link expired")
    if not Path(invoice.file_path).is_file():
        raise HTTPException(status_code=404, detail="The stored invoice file is missing")
    return invoice


@router.get("/invoices/{invoice_id}/file")
def get_invoice_file(
    invoice_id: str, token: str | None = Query(default=None), authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> FileResponse:
    invoice = _file_invoice(db, invoice_id, token, authorization)
    return FileResponse(
        invoice.file_path, media_type=invoice.content_type, filename=invoice.filename, content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/invoices/{invoice_id}/pages/{page}")
def get_invoice_page(
    invoice_id: str, page: int, token: str | None = Query(default=None), scale: float = Query(default=2.0, ge=0.5, le=4.0),
    authorization: str | None = Header(default=None), db: Session = Depends(get_db),
) -> Response:
    invoice = _file_invoice(db, invoice_id, token, authorization)
    if invoice.content_type != "application/pdf":
        raise HTTPException(status_code=404, detail="Only PDF documents have page images")
    with pymupdf.open(invoice.file_path) as document:
        if not 0 <= page < document.page_count:
            raise HTTPException(status_code=404, detail="Page not found")
        image = document[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.post("/invoices/{invoice_id}/extract", response_model=InvoiceResponse)
def extract_invoice_endpoint(invoice_id: str, context: Context = Depends(editor), db: Session = Depends(get_db)) -> InvoiceResponse:
    invoice = get_scoped_invoice(db, context, invoice_id)
    if not Path(invoice.file_path).is_file():
        raise HTTPException(status_code=404, detail="The stored invoice file is missing")
    try:
        extraction = extract_document(Path(invoice.file_path), invoice.content_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invoice document could not be extracted") from exc
    _discard_pending_booking(db, invoice, context.actor)
    apply_extraction(db, invoice, context.organization, context.actor, extraction)
    db.commit()
    db.refresh(invoice)
    return invoice_response(invoice)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceResponse)
def update_invoice(invoice_id: str, payload: InvoicePatch, context: Context = Depends(editor), db: Session = Depends(get_db)) -> InvoiceResponse:
    invoice = get_scoped_invoice(db, context, invoice_id)
    changes = payload.fields.model_dump(exclude_unset=True)
    if "line_items" in changes and changes["line_items"] is not None:
        changes["line_items"] = [{k: v for k, v in item.items() if v not in (None, "")} for item in changes["line_items"]]
    old_values = invoice.effective_data()
    actual = {key: value for key, value in changes.items() if (old_values.get(key) or None) != (value or None)}
    if actual:
        corrected = dict(invoice.corrected_data)
        for key, value in actual.items():
            if (invoice.original_data.get(key) or None) == (value or None):
                corrected.pop(key, None)
            else:
                corrected[key] = value
        invoice.corrected_data = corrected
        invoice.add_audit(
            "fields_corrected", context.actor,
            changes={key: {"from": old_values.get(key), "to": value} for key, value in actual.items()},
        )
        _discard_pending_booking(db, invoice, context.actor)
        run_checks(db, invoice, context.organization, context.actor)  # re-validate whenever fields change
    db.commit()
    db.refresh(invoice)
    return invoice_response(invoice)


@router.post("/invoices/{invoice_id}/validate", response_model=ValidationResponse)
def validate_invoice_endpoint(invoice_id: str, context: Context = Depends(editor), db: Session = Depends(get_db)) -> ValidationResponse:
    invoice = get_scoped_invoice(db, context, invoice_id)
    overall = run_checks(db, invoice, context.organization, context.actor)
    db.commit()
    return ValidationResponse(invoice_id=invoice.id, results=invoice.validation_results, overall_status=overall)  # type: ignore[arg-type]


@router.delete("/invoices/{invoice_id}", status_code=204)
def delete_invoice(invoice_id: str, context: Context = Depends(editor), db: Session = Depends(get_db)) -> Response:
    """GDPR-conscious deletion: removes the record, derived data and stored files. The audit log keeps
    only the action, actor and file name, not invoice contents."""
    invoice = get_scoped_invoice(db, context, invoice_id)
    paths = [Path(f.storage_path) for f in db.scalars(select(InvoiceFile).where(InvoiceFile.invoice_id == invoice.id))]
    paths.append(Path(invoice.file_path))
    from app.records import record_audit

    record_audit(db, invoice.organization_id, context.actor, "invoice", invoice.id, "deleted", filename=invoice.filename)
    counterparts = [str(i) for i in (invoice.field_meta or {}).get("_duplicate_ids", [])]
    db.delete(invoice)
    db.flush()
    recheck_duplicates_after_delete(db, context.organization, context.actor, counterparts)
    db.commit()
    storage = Path(settings.upload_dir).resolve()
    for path in paths:
        if path.resolve().parent == storage:
            path.unlink(missing_ok=True)
    return Response(status_code=204)


# --- Cross-border ---------------------------------------------------------------------------

def _seed_tax_rules(db: Session) -> None:
    existing = {(rule_id, version) for rule_id, version in db.execute(select(TaxRule.rule_id, TaxRule.version)).all()}
    for definition in cross_border.rule_definitions():
        if (definition["rule_id"], definition["version"]) not in existing:
            db.add(TaxRule(**definition))


@router.get("/tax-rules", tags=["cross-border"])
def list_tax_rules(_context: Context = Depends(get_context), db: Session = Depends(get_db)) -> list[dict[str, object]]:
    _seed_tax_rules(db)
    db.commit()
    rules = db.scalars(select(TaxRule).order_by(TaxRule.direction, TaxRule.rule_id)).all()
    return [
        {"rule_id": r.rule_id, "version": r.version, "jurisdiction": r.jurisdiction, "direction": r.direction, "supply_type": r.supply_type,
         "valid_from": r.valid_from.isoformat(), "valid_to": r.valid_to.isoformat() if r.valid_to else None, "active": r.active, **r.definition}
        for r in rules
    ]


def _party_ids(values: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for party in ("supplier", "customer"):
        gstin, vat_id = values.get(f"{party}_gstin"), values.get(f"{party}_vat_id")
        result[party] = {
            "name": values.get(f"{party}_name"),
            "address": values.get(f"{party}_address"),
            "country": values.get(f"{party}_country"),
            "gstin": gstin_details(str(gstin)) if gstin else None,
            "vat_id": {**vat_id_details(str(vat_id)), "registration": verify_vat_registration(str(vat_id)).__dict__} if vat_id else None,
        }
    return result


def _assessment_payload(invoice: Invoice) -> dict[str, object]:
    values = invoice.effective_data()
    result = cross_border.assess(values, invoice.transaction_facts or {}, invoice.field_meta)
    result["parties"] = _party_ids(values)
    result["currency"] = {
        "original_currency": values.get("currency"),
        "original_gross_amount": values.get("gross_amount"),
        "conversion": invoice.fx_conversion,
    }
    return result


@router.get("/invoices/{invoice_id}/cross-border", tags=["cross-border"])
def cross_border_preview(invoice_id: str, context: Context = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, object]:
    invoice = get_scoped_invoice(db, context, invoice_id)
    payload = _assessment_payload(invoice)
    latest = db.scalar(
        select(CrossBorderAssessment).where(CrossBorderAssessment.invoice_id == invoice.id).order_by(CrossBorderAssessment.created_at.desc())
    )
    payload["saved_assessment"] = (
        {"id": latest.id, "created_at": latest.created_at.isoformat(), "created_by": latest.created_by, "advisor_status": latest.advisor_status,
         "reviewed_by": latest.reviewed_by, "advisor_note": latest.advisor_note, "status": latest.status}
        if latest else None
    )
    return payload


@router.post("/invoices/{invoice_id}/cross-border", tags=["cross-border"], status_code=201)
def run_cross_border(invoice_id: str, context: Context = Depends(editor), db: Session = Depends(get_db)) -> dict[str, object]:
    """Runs and stores a versioned cross-border assessment (pending advisor review)."""
    invoice = get_scoped_invoice(db, context, invoice_id)
    _seed_tax_rules(db)
    payload = _assessment_payload(invoice)
    record = CrossBorderAssessment(
        organization_id=invoice.organization_id, invoice_id=invoice.id, ruleset_version=str(payload["ruleset_version"]),
        direction=str(payload["direction"]), transaction_type=payload.get("transaction_type"), status=str(payload["status"]),
        confidence=str(payload["confidence"]), result=payload, facts=dict(invoice.transaction_facts or {}), created_by=context.actor,
    )
    db.add(record)
    invoice.add_audit("cross_border_assessed", context.actor, status=payload["status"], direction=payload["direction"])
    db.commit()
    return {**payload, "saved_assessment": {"id": record.id, "created_by": record.created_by, "advisor_status": record.advisor_status,
                                            "created_at": record.created_at.isoformat(), "reviewed_by": None, "advisor_note": None, "status": record.status}}


@router.post("/invoices/{invoice_id}/cross-border/{assessment_id}/review", tags=["cross-border"])
def review_cross_border(
    invoice_id: str, assessment_id: str, payload: AdvisorReview, context: Context = Depends(editor), db: Session = Depends(get_db)
) -> dict[str, object]:
    invoice = get_scoped_invoice(db, context, invoice_id)
    record = db.get(CrossBorderAssessment, assessment_id)
    if not record or record.invoice_id != invoice.id:
        raise HTTPException(status_code=404, detail="Assessment not found")
    record.advisor_status = payload.status
    record.advisor_note = payload.note
    record.reviewed_by = context.actor
    record.reviewed_at = datetime.now(timezone.utc)
    invoice.add_audit("cross_border_reviewed", context.actor, advisor_status=payload.status)
    db.commit()
    return {"id": record.id, "advisor_status": record.advisor_status, "reviewed_by": record.reviewed_by, "advisor_note": record.advisor_note}


@router.put("/invoices/{invoice_id}/facts", response_model=InvoiceResponse, tags=["cross-border"])
def update_facts(invoice_id: str, payload: FactsUpdate, context: Context = Depends(editor), db: Session = Depends(get_db)) -> InvoiceResponse:
    invoice = get_scoped_invoice(db, context, invoice_id)
    facts = {key: value for key, value in payload.model_dump().items() if value is not None}
    if facts != (invoice.transaction_facts or {}):
        invoice.add_audit("facts_updated", context.actor, facts=facts)
        invoice.transaction_facts = facts
        db.commit()
        db.refresh(invoice)
    return invoice_response(invoice)


@router.post("/invoices/{invoice_id}/fx", response_model=InvoiceResponse, tags=["cross-border"])
def convert_currency(invoice_id: str, payload: FxRequest, context: Context = Depends(editor), db: Session = Depends(get_db)) -> InvoiceResponse:
    invoice = get_scoped_invoice(db, context, invoice_id)
    values = invoice.effective_data()
    currency = str(values.get("currency") or "").upper()
    if not currency or currency == "EUR":
        raise HTTPException(status_code=422, detail="Set a non-EUR invoice currency before converting")
    if currency not in fx.SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422, detail=f"{currency} is not supported; use EUR, INR, USD or GBP")
    rate_date = payload.rate_date
    if rate_date is None:
        try:
            rate_date = date.fromisoformat(str(values.get("invoice_date")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Provide a rate date or a valid invoice date") from exc
    try:
        if payload.provider == "manual":
            if payload.units_per_eur is None:
                raise fx.FxError("Enter the rate as units of the invoice currency per 1 EUR")
            rate = fx.manual_rate(currency, payload.units_per_eur, rate_date, payload.source or "")
        elif payload.provider == "demo":
            rate = fx.demo_rate(currency, rate_date)
        else:
            rate = fx.ecb_rate(currency, rate_date)
    except fx.FxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    fx.store_rate(db, rate)
    invoice.fx_conversion = fx.convert(values, rate)
    corrected = dict(invoice.corrected_data)
    corrected.update(
        exchange_rate=str(rate.units_per_eur), exchange_rate_date=rate.rate_date.isoformat(),
        exchange_rate_source=rate.source, exchange_rate_status="provided",
    )
    invoice.corrected_data = corrected
    invoice.add_audit("currency_converted", context.actor, provider=rate.provider, rate=str(rate.units_per_eur), rate_date=rate.rate_date.isoformat())
    _discard_pending_booking(db, invoice, context.actor)
    run_checks(db, invoice, context.organization, context.actor)
    db.commit()
    db.refresh(invoice)
    return invoice_response(invoice)


@router.post("/organization/retention/purge", tags=["organization"])
def purge_expired(context: Context = Depends(editor), db: Session = Depends(get_db)) -> dict[str, int]:
    """Deletes invoices older than the organization's retention period (GDPR storage limitation)."""
    if context.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can purge data")
    cutoff = datetime.now(timezone.utc).timestamp() - context.organization.retention_days * 86400
    expired = [
        invoice for invoice in db.scalars(select(Invoice).where(Invoice.organization_id == context.organization.id))
        if invoice.created_at.timestamp() < cutoff
    ]
    storage = Path(settings.upload_dir).resolve()
    for invoice in expired:
        for path in Path(invoice.file_path).parent.glob(f"{invoice.id}*"):
            if path.resolve().parent == storage:
                path.unlink(missing_ok=True)
        db.delete(invoice)
    from app.records import record_audit

    record_audit(db, context.organization.id, context.actor, "organization", context.organization.id, "retention_purge", deleted=len(expired))
    db.commit()
    return {"deleted": len(expired)}


def compliance_of(invoice: Invoice) -> str:
    return compliance_status(invoice.validation_results)


def count_invoices(db: Session, organization_id: str) -> int:
    return int(db.scalar(select(func.count()).select_from(Invoice).where(Invoice.organization_id == organization_id)) or 0)

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounting import SuggestionError, build_booking, invoice_direction, match_mapping
from app.api_schemas import ChartAccountInput, ChartAccountResponse, MappingInput, MappingResponse, SuggestionResponse
from app.auth import Context, admin, approver, editor, get_context
from app.db import get_db
from app.models import AccountMapping, BookingSuggestion, ChartAccount, Invoice
from app.records import record_audit
from app.routers.invoices import get_scoped_invoice

router = APIRouter(prefix="/api", tags=["accounting"])


def suggestion_response(suggestion: BookingSuggestion, invoice: Invoice) -> SuggestionResponse:
    return SuggestionResponse.model_validate(
        {**{c.name: getattr(suggestion, c.name) for c in BookingSuggestion.__table__.columns}, "invoice_filename": invoice.filename}
    )


def _mapping(db: Session, context: Context, mapping_id: str) -> AccountMapping:
    mapping = db.get(AccountMapping, mapping_id)
    if not mapping or mapping.organization_id != context.organization.id:
        raise HTTPException(status_code=404, detail="Mapping not found")
    return mapping


# --- Chart of accounts ------------------------------------------------------------------------

@router.get("/accounting/chart", response_model=list[ChartAccountResponse])
def list_chart(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> list[ChartAccount]:
    return list(db.scalars(select(ChartAccount).where(ChartAccount.organization_id == context.organization.id).order_by(ChartAccount.number)))


@router.post("/accounting/chart", response_model=ChartAccountResponse, status_code=201)
def add_chart_account(payload: ChartAccountInput, context: Context = Depends(admin), db: Session = Depends(get_db)) -> ChartAccount:
    account = ChartAccount(organization_id=context.organization.id, chart=context.organization.chart_of_accounts, **payload.model_dump())
    db.add(account)
    record_audit(db, context.organization.id, context.actor, "chart_account", None, "created", number=payload.number)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="This account number already exists") from exc
    return account


@router.delete("/accounting/chart/{account_id}", status_code=204)
def delete_chart_account(account_id: str, context: Context = Depends(admin), db: Session = Depends(get_db)) -> Response:
    account = db.get(ChartAccount, account_id)
    if not account or account.organization_id != context.organization.id:
        raise HTTPException(status_code=404, detail="Account not found")
    db.delete(account)
    record_audit(db, context.organization.id, context.actor, "chart_account", account_id, "deleted", number=account.number)
    db.commit()
    return Response(status_code=204)


# --- Mappings -------------------------------------------------------------------------------------

@router.get("/accounting/mappings", response_model=list[MappingResponse])
def list_mappings(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> list[AccountMapping]:
    return list(db.scalars(
        select(AccountMapping).where(AccountMapping.organization_id == context.organization.id).order_by(AccountMapping.priority, AccountMapping.name)
    ))


@router.post("/accounting/mappings", response_model=MappingResponse, status_code=201)
def create_mapping(payload: MappingInput, context: Context = Depends(admin), db: Session = Depends(get_db)) -> AccountMapping:
    mapping = AccountMapping(organization_id=context.organization.id, **payload.model_dump())
    db.add(mapping)
    db.flush()
    record_audit(db, context.organization.id, context.actor, "account_mapping", mapping.id, "created", name=payload.name)
    db.commit()
    return mapping


@router.put("/accounting/mappings/{mapping_id}", response_model=MappingResponse)
def update_mapping(mapping_id: str, payload: MappingInput, context: Context = Depends(admin), db: Session = Depends(get_db)) -> AccountMapping:
    mapping = _mapping(db, context, mapping_id)
    for key, value in payload.model_dump().items():
        setattr(mapping, key, value)
    mapping.is_demo = False
    record_audit(db, context.organization.id, context.actor, "account_mapping", mapping.id, "updated", name=payload.name)
    db.commit()
    return mapping


@router.delete("/accounting/mappings/{mapping_id}", status_code=204)
def delete_mapping(mapping_id: str, context: Context = Depends(admin), db: Session = Depends(get_db)) -> Response:
    mapping = _mapping(db, context, mapping_id)
    record_audit(db, context.organization.id, context.actor, "account_mapping", mapping.id, "deleted", name=mapping.name)
    db.delete(mapping)
    db.commit()
    return Response(status_code=204)


# --- Suggestions --------------------------------------------------------------------------------

@router.get("/accounting/suggestions", response_model=list[SuggestionResponse])
def list_suggestions(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> list[SuggestionResponse]:
    rows = db.execute(
        select(BookingSuggestion, Invoice).join(Invoice, Invoice.id == BookingSuggestion.invoice_id)
        .where(BookingSuggestion.organization_id == context.organization.id).order_by(BookingSuggestion.created_at.desc())
    ).all()
    return [suggestion_response(suggestion, invoice) for suggestion, invoice in rows]


def _suggest(invoice_id: str, context: Context, db: Session) -> SuggestionResponse:
    invoice = get_scoped_invoice(db, context, invoice_id)
    existing = db.scalar(select(BookingSuggestion).where(BookingSuggestion.invoice_id == invoice.id))
    if existing and existing.status in ("approved", "exported"):
        raise HTTPException(status_code=409, detail=f"This invoice already has an {existing.status} booking")
    mappings = list(db.scalars(select(AccountMapping).where(AccountMapping.organization_id == context.organization.id)))
    direction = invoice_direction(invoice.effective_data(), context.organization)
    try:
        mapping, keyword, matched_on = match_mapping(invoice.effective_data(), mappings, direction)
        booking, explanation = build_booking(invoice, mapping, keyword, matched_on, direction)
    except SuggestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    suggestion = existing or BookingSuggestion(organization_id=context.organization.id, invoice_id=invoice.id)
    suggestion.mapping_id = mapping.id
    suggestion.booking = booking
    suggestion.explanation = explanation
    suggestion.status = "proposed"
    suggestion.created_by = context.actor
    suggestion.decided_by = None
    suggestion.decided_at = None
    db.add(suggestion)
    invoice.add_audit("booking_suggested", context.actor, mapping=mapping.name, account=booking["account"], contra_account=booking["contra_account"])
    db.commit()
    return suggestion_response(suggestion, invoice)


@router.post("/accounting/invoices/{invoice_id}/suggestion", response_model=SuggestionResponse, status_code=201)
def suggest_booking(invoice_id: str, context: Context = Depends(editor), db: Session = Depends(get_db)) -> SuggestionResponse:
    return _suggest(invoice_id, context, db)


@router.post("/invoices/{invoice_id}/accounting", response_model=SuggestionResponse, status_code=201)
def invoice_accounting(invoice_id: str, context: Context = Depends(editor), db: Session = Depends(get_db)) -> SuggestionResponse:
    """Generates an accounting suggestion for the invoice (no posting)."""
    return _suggest(invoice_id, context, db)


def _decide(suggestion: BookingSuggestion | None, status: str, context: Context, db: Session) -> SuggestionResponse:
    if not suggestion or suggestion.organization_id != context.organization.id:
        raise HTTPException(status_code=404, detail="Booking suggestion not found")
    if suggestion.status not in ("proposed", "approved", "rejected") or suggestion.status == status:
        raise HTTPException(status_code=409, detail=f"A {suggestion.status} booking cannot be {status}")
    invoice = db.get(Invoice, suggestion.invoice_id)
    assert invoice is not None
    suggestion.status = status
    suggestion.decided_by = context.actor
    suggestion.decided_at = datetime.now(timezone.utc)
    if status == "approved":
        invoice.status = "approved"
    elif invoice.status == "approved":
        invoice.status = "needs_review"
    invoice.add_audit(f"booking_{status}", context.actor, account=suggestion.booking.get("account"))
    db.commit()
    return suggestion_response(suggestion, invoice)


@router.post("/accounting/suggestions/{suggestion_id}/approve", response_model=SuggestionResponse)
def approve(suggestion_id: str, context: Context = Depends(approver), db: Session = Depends(get_db)) -> SuggestionResponse:
    return _decide(db.get(BookingSuggestion, suggestion_id), "approved", context, db)


@router.post("/accounting/suggestions/{suggestion_id}/reject", response_model=SuggestionResponse)
def reject(suggestion_id: str, context: Context = Depends(approver), db: Session = Depends(get_db)) -> SuggestionResponse:
    return _decide(db.get(BookingSuggestion, suggestion_id), "rejected", context, db)


@router.post("/invoices/{invoice_id}/approve", response_model=SuggestionResponse)
def approve_invoice(invoice_id: str, context: Context = Depends(approver), db: Session = Depends(get_db)) -> SuggestionResponse:
    """Accountant approval of the invoice's booking suggestion."""
    invoice = get_scoped_invoice(db, context, invoice_id)
    suggestion = db.scalar(select(BookingSuggestion).where(BookingSuggestion.invoice_id == invoice.id))
    if suggestion is None:
        raise HTTPException(status_code=409, detail="Generate an accounting suggestion before approving")
    return _decide(suggestion, "approved", context, db)

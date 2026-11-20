from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api_schemas import ExportBatchResponse, ExportPreview, ExportPreviewRow
from app.auth import Context, approver, get_context
from app.datev import PROTOTYPE_LABEL, booking_errors, build_csv, organization_errors
from app.records import record_audit
from app.db import get_db
from app.models import BookingSuggestion, ExportBatch, Invoice
from app.routers.accounting import suggestion_response

router = APIRouter(prefix="/api/exports", tags=["exports"])
spec_router = APIRouter(prefix="/api/export", tags=["exports"])
DISCLAIMER = PROTOTYPE_LABEL


def _approved(db: Session, context: Context) -> list[tuple[BookingSuggestion, Invoice]]:
    return [
        (suggestion, invoice)
        for suggestion, invoice in db.execute(
            select(BookingSuggestion, Invoice)
            .join(Invoice, Invoice.id == BookingSuggestion.invoice_id)
            .where(BookingSuggestion.organization_id == context.organization.id, BookingSuggestion.status == "approved")
            .order_by(BookingSuggestion.created_at)
        ).all()
    ]


@router.get("/datev/preview", response_model=ExportPreview)
def preview(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> ExportPreview:
    rows = [
        ExportPreviewRow(suggestion=suggestion_response(s, i), errors=booking_errors(s, context.organization))
        for s, i in _approved(db, context)
    ]
    org_errors = organization_errors(context.organization)
    return ExportPreview(
        organization_errors=org_errors,
        rows=rows,
        exportable=0 if org_errors else sum(not row.errors for row in rows),
        disclaimer=DISCLAIMER,
    )


@router.post("/datev", response_model=ExportBatchResponse, status_code=201)
def create_export(context: Context = Depends(approver), db: Session = Depends(get_db)) -> ExportBatch:
    org_errors = organization_errors(context.organization)
    if org_errors:
        raise HTTPException(status_code=422, detail=" ".join(org_errors))
    valid = [(s, i) for s, i in _approved(db, context) if not booking_errors(s, context.organization)]
    if not valid:
        raise HTTPException(status_code=422, detail="There are no approved bookings without validation errors to export")
    try:
        content, filename = build_csv(context.organization, [s for s, _ in valid])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    batch = ExportBatch(
        organization_id=context.organization.id,
        filename=filename,
        content=content,
        booking_count=len(valid),
        created_by=context.actor,
    )
    db.add(batch)
    db.flush()
    for suggestion, invoice in valid:
        suggestion.status = "exported"
        suggestion.export_batch_id = batch.id
        invoice.status = "exported"
        invoice.add_audit("booking_exported", context.actor, export_batch_id=batch.id)
    record_audit(db, context.organization.id, context.actor, "datev_export", batch.id, "created", bookings=len(valid))
    db.commit()
    return batch


@router.get("/datev", response_model=list[ExportBatchResponse])
def list_exports(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> list[ExportBatch]:
    return list(
        db.scalars(
            select(ExportBatch)
            .where(ExportBatch.organization_id == context.organization.id)
            .order_by(ExportBatch.created_at.desc())
        )
    )


@router.get("/datev/{batch_id}/download")
def download(batch_id: str, context: Context = Depends(get_context), db: Session = Depends(get_db)) -> Response:
    batch = db.get(ExportBatch, batch_id)
    if not batch or batch.organization_id != context.organization.id:
        raise HTTPException(status_code=404, detail="Export not found")
    return Response(
        content=batch.content.encode("cp1252", errors="replace"),
        media_type="text/csv; charset=windows-1252",
        headers={"Content-Disposition": f'attachment; filename="{batch.filename}"', "Cache-Control": "private, no-store"},
    )


@spec_router.post("/datev", response_model=ExportBatchResponse, status_code=201)
def create_export_spec(context: Context = Depends(approver), db: Session = Depends(get_db)) -> ExportBatch:
    return create_export(context, db)


@spec_router.get("/{batch_id}/download")
def download_spec(batch_id: str, context: Context = Depends(get_context), db: Session = Depends(get_db)) -> Response:
    return download(batch_id, context, db)


@spec_router.get("/{batch_id}/preview")
def preview_batch(batch_id: str, context: Context = Depends(get_context), db: Session = Depends(get_db)) -> dict[str, object]:
    batch = db.get(ExportBatch, batch_id)
    if not batch or batch.organization_id != context.organization.id:
        raise HTTPException(status_code=404, detail="Export not found")
    return {"filename": batch.filename, "content": batch.content, "label": DISCLAIMER}

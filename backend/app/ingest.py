"""Invoice ingestion and processing pipeline shared by uploads, the demo loader and future
channels (e.g. email). Store -> extract -> record -> sync -> validate."""

import hashlib
import logging
import os
from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as db_module
from app.extraction import MIN_TEXT_CHARACTERS, Extraction, extract_document
from app.models import ExtractionResult, Invoice, InvoiceFile, Organization
from app.records import compliance_status, find_duplicates, store_validation_results, sync_invoice
from app.settings import settings
from app.validation import validate_invoice

logger = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {".pdf": "application/pdf", ".xml": "application/xml"}
STATUS_BY_RESULT = {"FAIL": "validation_failed", "NEEDS_REVIEW": "needs_review", "PASS": "validated", "NOT_CHECKED": "needs_review"}


def check_upload(filename: str, content: bytes) -> tuple[str, str]:
    """Returns (safe filename, content type) or raises 413/415."""
    name = Path(filename or "").name
    extension = Path(name).suffix.casefold()
    content_type = ALLOWED_EXTENSIONS.get(extension)
    if not name or not content_type:
        raise HTTPException(status_code=415, detail="Only PDF, XML, XRechnung and ZUGFeRD invoice files are supported")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"{name} exceeds the 15 MB upload limit")
    is_pdf = content_type == "application/pdf" and content.startswith(b"%PDF-")
    is_xml = content_type == "application/xml" and content.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<")
    if not (is_pdf or is_xml):
        raise HTTPException(status_code=415, detail=f"{name} does not match its file extension")
    return name[:255], content_type


def needs_background_ocr(path: Path) -> bool:
    try:
        with pymupdf.open(path) as document:
            text = "".join(page.get_text() for page in document)
            return len("".join(text.split())) < MIN_TEXT_CHARACTERS and not document.embfile_count()
    except Exception:
        return False


def store_file(organization_id: str, filename: str, content_type: str, content: bytes) -> tuple[str, Path, str]:
    file_id = str(uuid4())
    storage_dir = Path(settings.upload_dir).resolve()
    storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(storage_dir, 0o700)
    path = storage_dir / f"{file_id}{Path(filename).suffix.casefold()}"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    return file_id, path, hashlib.sha256(content).hexdigest()


def create_invoice(
    db: Session, organization: Organization, actor: str, filename: str, content: bytes, channel: str = "upload"
) -> tuple[Invoice, bool, bool]:
    """Returns (invoice, is_exact_duplicate, needs_background_processing)."""
    filename, content_type = check_upload(filename, content)
    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(Invoice).where(Invoice.organization_id == organization.id, Invoice.sha256 == digest))
    if existing:
        existing.add_audit("duplicate_upload_detected", actor, filename=filename, channel=channel)
        return existing, True, False
    file_id, path, digest = store_file(organization.id, filename, content_type, content)
    invoice = Invoice(
        id=file_id, organization_id=organization.id, filename=filename, content_type=content_type, file_path=str(path),
        file_size=len(content), sha256=digest, source_channel=channel, status="processing", original_data={}, corrected_data={},
        validation_results=[], audit_history=[], transaction_facts={}, field_meta={}, pages=[],
    )
    invoice.add_audit("uploaded", actor, channel=channel, size=len(content))
    db.add(invoice)
    db.add(InvoiceFile(organization_id=organization.id, invoice_id=file_id, kind="original", filename=filename,
                       content_type=content_type, storage_path=str(path), size=len(content), sha256=digest))
    db.flush()
    background = content_type == "application/pdf" and needs_background_ocr(path)
    if not background:
        try:
            extraction = extract_document(path, content_type)
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"{filename} could not be read as a valid invoice document") from exc
        apply_extraction(db, invoice, organization, actor, extraction)
    return invoice, False, background


def apply_extraction(db: Session, invoice: Invoice, organization: Organization, actor: str, extraction: Extraction) -> None:
    invoice.original_data = extraction.fields
    invoice.field_meta = extraction.field_meta
    invoice.pages = extraction.pages
    invoice.extraction_method = extraction.method
    invoice.extraction_provider = extraction.provider
    invoice.extracted_text = extraction.text[:200_000]
    db.add(ExtractionResult(organization_id=organization.id, invoice_id=invoice.id, method=extraction.method,
                            provider=extraction.provider, fields=extraction.fields, field_meta=extraction.field_meta,
                            warnings=extraction.warnings, created_by=actor))
    if extraction.embedded_xml:
        xml_path = Path(invoice.file_path).with_suffix(".embedded.xml")
        xml_path.write_bytes(extraction.embedded_xml)
        os.chmod(xml_path, 0o600)
        db.add(InvoiceFile(organization_id=organization.id, invoice_id=invoice.id, kind="embedded_xml", filename="factur-x.xml",
                           content_type="application/xml", storage_path=str(xml_path), size=len(extraction.embedded_xml),
                           sha256=hashlib.sha256(extraction.embedded_xml).hexdigest()))
    invoice.add_audit("extracted", actor, method=extraction.method, provider=extraction.provider, warnings=extraction.warnings)
    run_checks(db, invoice, organization, actor)


def run_checks(db: Session, invoice: Invoice, organization: Organization, actor: str, recheck_counterparts: bool = True) -> str:
    db.flush()
    sync_invoice(db, invoice, organization)
    db.flush()
    duplicates = find_duplicates(db, invoice)
    previous = {d["id"] for d in _flagged_duplicates(invoice)}
    results = validate_invoice(invoice.effective_data(), duplicates=duplicates)
    invoice.validation_results = results
    invoice.field_meta = {**(invoice.field_meta or {}), "_duplicate_ids": [d["id"] for d in duplicates]}
    store_validation_results(db, invoice, results)
    overall = compliance_status(results)
    invoice.compliance_status = overall
    if invoice.status not in ("approved", "exported"):
        invoice.status = STATUS_BY_RESULT[overall]
    invoice.add_audit("checks_run", actor, overall_status=overall)
    if recheck_counterparts:
        # Duplicates are symmetric: re-check invoices that became (or stopped being) duplicates of this one.
        for other_id in {d["id"] for d in duplicates} | previous:
            other = db.get(Invoice, other_id)
            if other is not None and other.id != invoice.id:
                run_checks(db, other, organization, actor, recheck_counterparts=False)
    return overall


def _flagged_duplicates(invoice: Invoice) -> list[dict[str, object]]:
    return [{"id": d} for d in (invoice.field_meta or {}).get("_duplicate_ids", [])] if isinstance(invoice.field_meta, dict) else []


def recheck_duplicates_after_delete(db: Session, organization: Organization, actor: str, invoice_ids: list[str]) -> None:
    for invoice_id in invoice_ids:
        other = db.get(Invoice, invoice_id)
        if other is not None:
            run_checks(db, other, organization, actor, recheck_counterparts=False)


def process_in_background(invoice_id: str, actor: str) -> None:
    """Runs OCR extraction for scanned PDFs outside the upload request."""
    with db_module.SessionLocal() as db:
        invoice = db.get(Invoice, invoice_id)
        if invoice is None:
            return
        organization = db.get(Organization, invoice.organization_id)
        assert organization is not None
        try:
            extraction = extract_document(Path(invoice.file_path), invoice.content_type)
            apply_extraction(db, invoice, organization, actor, extraction)
        except Exception:
            logger.exception("Background extraction failed for invoice %s", invoice_id)
            invoice.status = "extraction_failed"
            invoice.add_audit("extraction_failed", actor)
        db.commit()

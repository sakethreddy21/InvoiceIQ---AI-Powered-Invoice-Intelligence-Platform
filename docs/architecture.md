# InvoiceIQ architecture

## Product boundary

InvoiceIQ assists finance teams with invoice intake, review, deterministic checks and accounting preparation. It does not issue tax or legal opinions and never posts accounting entries. Cross-border results are indicators based on facts a person confirms, and remain pending until an advisor reviews them. AI output (OCR, optional LLM) is treated as an extraction suggestion with a confidence score, never as a compliance decision.

## Runtime

```text
Browser ── Next.js App Router (apps/web, client components + TanStack Query, shadcn/ui)
              │  bearer token, X-Organization-ID
              ▼
           FastAPI (backend/app)
              ├── PostgreSQL (Alembic migrations, organization-scoped data, Decimal amounts)
              ├── Private file storage (storage/invoices, 0600 files, signed preview links)
              ├── Background tasks (OCR of scanned PDFs; Celery/Redis optional for scale-out)
              └── Optional providers: PaddleOCR/Tesseract (local), LLM (Anthropic), ECB rates, EU VIES
```

## Backend modules

| Module | Responsibility |
|---|---|
| `routers/` | HTTP contracts: auth/organization, invoices (upload, review, validation, cross-border, FX), accounting, exports, demo |
| `auth.py`, `security.py` | Token auth, organization context, role checks; scrypt hashing and HMAC tokens |
| `ingest.py` | Shared pipeline: validate → store → extract → record → sync → validate; background OCR |
| `extraction.py`, `einvoice.py`, `ocr.py`, `llm.py` | Text/layout rules, XRechnung/UBL/CII/ZUGFeRD parsing, OCR providers, optional LLM gap filling; field confidence and locations |
| `validation.py`, `tax_ids.py` | Deterministic German/Indian formal checks; GSTIN, VAT-ID, VIES/BZSt interfaces |
| `cross_border.py`, `fx.py` | Versioned effective-dated rules, classification, assessments; ECB/manual/demo rates with provenance |
| `accounting.py`, `datev.py` | Mapping-based suggestions; EXTF Buchungsstapel prototype and validation |
| `records.py`, `models.py` | Normalized tables (parties, line items, tax details, validation results, files, extraction runs, assessments, exchange rates, chart of accounts, tax rules, audit logs) |

## Data and trust boundaries

- Every business query filters by organization; other organizations' records return 404.
- Uploaded files are untrusted: extension, magic bytes, size and count limits are enforced; XML is parsed without entities, DTDs or network access.
- Logs contain no invoice contents; the audit log stores actions, actors and changed field names/values inside the database.
- Monetary values are `Decimal` in code and `NUMERIC` in the database.
- Retention: organizations set a retention period; purge deletes invoices, derived rows and files.
- External processing (LLM, VIES) is opt-in via environment variables and documented in Settings.

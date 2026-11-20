# InvoiceIQ

InvoiceIQ is a full-stack invoice intelligence platform for accounting firms, finance teams and businesses operating between Germany and India. It covers the complete workflow:

**Invoice upload → data extraction → human review → German VAT compliance → India–Germany cross-border analysis → accounting suggestions → DATEV export.**

InvoiceIQ never makes legal determinations, never invents account numbers or tax keys, and never posts bookings automatically. Every cross-border result is an indicator that stays pending until a tax advisor reviews it.

## Architecture

- `apps/web` — Next.js App Router, TypeScript, Tailwind CSS, shadcn/ui, Lucide icons, TanStack Query, Playwright.
- `backend` — FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL; PyMuPDF, lxml, PaddleOCR (optional), optional LLM provider.
- `docker-compose.yml` — PostgreSQL and Redis for local development. Scanned-PDF OCR runs as a FastAPI background task; Redis/Celery can take this over for multi-worker deployments.
- `demo_invoices/` — the six fictional demo documents (PDF, ZUGFeRD hybrid PDF, XRechnung XML).

See [docs/architecture.md](./docs/architecture.md) and [docs/implementation-plan.md](./docs/implementation-plan.md).

## Run locally

1. Copy `.env.example` to `.env` and set `SECRET_KEY` (the generator command is in the file).
2. Start PostgreSQL and Redis: `docker compose up -d`.
3. Start the API. Migrations run automatically on startup (or run `alembic upgrade head` in `backend/`).

   ```sh
   cd backend
   python -m venv .venv && . .venv/bin/activate
   pip install -e '.[test]'          # core
   pip install -e '.[ocr]'           # optional: PaddleOCR for scanned PDFs (large)
   pip install -e '.[llm]'           # optional: LLM-assisted extraction
   uvicorn app.main:app --reload --port 8001
   ```

4. Start the web app: `cd apps/web && cp .env.example .env.local && npm install && npm run dev`.
5. Open `http://localhost:3000`, choose **Create a workspace**, then **Settings → Open demo workspace** to load the six demo invoices into a separate, clearly labelled organization. Inside the demo workspace, **Reset demo** (top bar) restores it to its starting state before the next presentation; real workspaces cannot be reset.
6. A backup of the demo as a PDF (screenshots plus talking points) is in `docs/demo-walkthrough/InvoiceIQ-demo-walkthrough.pdf`. To regenerate it after UI changes: in `apps/web`, run `WALKTHROUGH=1 npx playwright test demo-walkthrough` then `node e2e/build-walkthrough-guide.mjs`.

API documentation: `http://localhost:8001/docs`. Health: `GET /api/health`; database readiness: `GET /api/ready`.

**Works without API keys.** Extraction, OCR, validation, cross-border rules and exports run locally. Exchange rates come from the ECB (live) or — offline — from labelled demo rates or manual rates with a stated source. LLM extraction and VIES lookups are off unless configured.

## Features

| Area | What works |
|---|---|
| Inbox | Drag-and-drop multi-file upload (PDF, scanned PDF, XRechnung/UBL XML, CII XML, ZUGFeRD/Factur-X hybrid PDF); type/size/content validation; SHA-256 hashing; duplicate file detection; processing status; search by supplier/number; filters for date, currency, compliance status and transaction type; email-forwarding interface clearly marked *not connected*. |
| Extraction | Native XML for e-invoices (preferred over OCR/text); embedded ZUGFeRD XML; text PDFs with label and position-aware rules; scanned PDFs via PaddleOCR (Tesseract fallback); optional LLM gap-filling with Pydantic-validated structured output. Extracts parties, VAT IDs, GSTINs, dates (invoice, due, delivery/service), currency, amounts, rates, line items, payment details (IBAN/BIC/terms), reverse-charge wording and exemption references, with per-field confidence and page location. |
| Review | Split screen: page images with zoom, page navigation and highlighted extraction regions; editable fields with confidence scores; editable line items; original vs corrected values stored separately; checks re-run on every change; full audit history. |
| German compliance | Deterministic Decimal-based rules with rule ID, result (PASS/FAIL/NEEDS_REVIEW), severity, explanation, detected/expected values, suggested correction and legal reference: §14 UStG fields, §33 UStDV small-amount invoices, description, service date, VAT rate plausibility, VAT and gross arithmetic, exemption references, §13b/§14a reverse-charge wording, credit-note references, unique invoice numbers and duplicates, VAT-ID format and German check digit. Registration checks (VIES; BZSt interface) are separate from format checks. |
| Cross-border | Classifies India→Germany, Germany→India, domestic German, domestic Indian and other international transactions. GSTIN structure, state code, PAN format, entity code and checksum. EUR/INR/USD/GBP conversion recording rate, date and source. Versioned, effective-dated rules (stored in `tax_rules`) separating goods and services and B2B/B2C: §13b and §3a UStG, export-of-goods exemption, non-EU service treatment, Indian export/import of services, Indian reverse charge, import VAT and customs. Each assessment shows direction, type, potential treatment, jurisdiction, reasoning, references, missing information, confidence, recommended advisor actions and advisor review status. |
| Accounting | Configurable SKR03/SKR04/custom chart of accounts and keyword mappings (purchase and sales); suggestions with account, counter-account, tax treatment, BU key, net/VAT/gross, booking text, reasoning, confidence and approval status. Nothing is posted. |
| DATEV | EXTF 700 Buchungsstapel (`EXTF_Buchungsstapel.csv`, Windows-1252, semicolons, decimal comma, DDMM dates) with currency/exchange-rate columns and partner details; validation and error reporting before export; preview and download. **DATEV-ready prototype — real DATEV import compatibility has not yet been verified.** |
| Security | Authentication (scrypt, signed tokens, login throttling), admin/accountant/viewer roles, organization isolation on every query, private file storage with short-lived signed preview links, upload restrictions, input validation, audit logs (`audit_logs` table), no invoice contents in logs, configurable retention with purge, full deletion of invoice files and derived data. |

## Tests

- Backend: `cd backend && pytest` — unit tests (PDF/XML/ZUGFeRD extraction, OCR, GSTIN structure and checksum, VAT-ID checks, VAT calculations, duplicates, validation, classification, currency conversion, DATEV formatting) and integration tests (auth/isolation, the complete six-demo-invoice workflow, migrations).
- Frontend: `cd apps/web && npm run lint && npm run build`.
- End to end: `cd apps/web && npx playwright install chromium && npm run test:e2e` — starts an isolated API (SQLite) and web build, then signs up, loads the demo workspace, corrects an invoice, saves a cross-border assessment, approves bookings and downloads the DATEV file.

## Demo dataset

`demo_invoices/` (all fictional):

1. Valid German domestic invoice with 19 % VAT (PDF).
2. German invoice with incorrect VAT arithmetic (PDF).
3. Indian supplier to German business, B2B service, INR (PDF).
4. German supplier to Indian business, consulting (ZUGFeRD hybrid PDF with embedded CII XML).
5. Indian invoice with a malformed GSTIN (PDF).
6. Duplicate of invoice 1, resent as XRechnung XML.

The original eight fixtures in `InvoiceIQ_test_invoices/` remain part of the automated tests.

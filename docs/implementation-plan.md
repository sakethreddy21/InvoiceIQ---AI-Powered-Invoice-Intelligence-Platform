# InvoiceIQ implementation plan and status

| Stage | Scope | Status |
|---|---|---|
| 1. Setup | Next.js + FastAPI + PostgreSQL + navigation, auth, organizations, Alembic | Complete |
| 2. Upload & processing | Multi-file upload, validation, SHA-256, duplicates, private storage, background OCR, filters | Complete |
| 3. Extraction & review | XML/ZUGFeRD/PDF/OCR/optional LLM extraction, confidence, split-screen viewer with zoom/pages/highlights, corrections, audit | Complete |
| 4. German compliance | §14 UStG, §33 UStDV, §13b/§14a wording, exemptions, VAT-ID checks, duplicates, VIES/BZSt interfaces | Complete (VIES opt-in; BZSt interface only) |
| 5. India–Germany intelligence | Classification, GSTIN, currencies with provenance, versioned rules, assessments with advisor review | Complete |
| 6. Accounting & DATEV | SKR03/SKR04/custom chart, mappings, suggestions, approval, EXTF prototype export | Complete (prototype, not DATEV-verified) |
| 7. Testing, demo, polish | Six demo invoices (PDF/XML/ZUGFeRD), pytest unit+integration, Playwright journey | Complete |

## Acceptance criteria

| # | Criterion | Where |
|---|---|---|
| 1 | Upload a PDF invoice | Inbox; `POST /api/invoices/upload` |
| 2 | Extract structured data | `extraction.py`, `einvoice.py`, `ocr.py` |
| 3 | Original and fields side by side | Extraction & Review page |
| 4 | Correct extracted information | `PATCH /api/invoices/{id}` (original vs corrected, re-validates) |
| 5 | German rules return meaningful results | `validation.py`, Compliance centre |
| 6 | Duplicate invoices identified | SHA-256 + supplier/number rule `DUPLICATE-INVOICE` (symmetric) |
| 7 | GSTIN structural validation | `tax_ids.gstin_details`, Cross-border page |
| 8 | Cross-border reasoning | `cross_border.assess` indicators with reasoning and references |
| 9 | Currency conversion records rate and date | `fx.py`, `exchange_rates` table |
| 10 | Accounting recommendations | `POST /api/invoices/{id}/accounting` |
| 11 | DATEV prototype download | `POST /api/export/datev`, `GET /api/export/{id}/download` |
| 12 | All six demo invoices processed | `test_demo_workspace_processes_all_six_invoices`, Playwright |
| 13 | Works without external API keys | Local OCR/rules; ECB or labelled demo rates; LLM/VIES off by default |
| 14 | No placeholder pages | All eight navigation sections are functional |

## Known limitations

- DATEV import compatibility has not been verified against a DATEV system; only the leading Buchungsstapel columns are written.
- Registration status of VAT IDs (VIES, opt-in) and GSTINs (no public API connected) is not verified by default; BZSt qualified confirmation is an interface only.
- Email ingestion is an interface; no inbound mailbox is connected.
- Cross-border rules cover the main India–Germany cases; special regimes (e.g. OIDAR registration details, construction §13b cases, customs valuation) need advisor input.
- Demo account mappings use SKR03 numbers marked "demo — verify with your accountant".

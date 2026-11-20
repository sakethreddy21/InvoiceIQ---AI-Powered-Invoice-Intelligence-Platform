InvoiceIQ demo invoices - ALL FICTIONAL

Every company, address, tax identifier, bank account and amount in this folder is fabricated
for software demonstrations. These are not valid invoices and must not be used for payment or tax filing.
Identifiers are generated to pass their checksums, except the intentionally malformed GSTIN in DEMO-05.

Files:
- DEMO-01_valid_german_19pct.pdf: Valid German domestic invoice with 19% VAT
- DEMO-02_incorrect_vat_math.pdf: German invoice with incorrect VAT arithmetic
- DEMO-03_india_to_germany_services.pdf: Indian supplier to German business, B2B services
- DEMO-04_germany_to_india_consulting_zugferd.pdf: German supplier to Indian business, consulting (ZUGFeRD hybrid PDF)
- DEMO-05_indian_malformed_gstin.pdf: Indian invoice with malformed GSTIN
- DEMO-06_duplicate_of_DEMO-01_xrechnung.xml: Duplicate of invoice 1, resent as XRechnung XML

Load them all at once with Settings -> Create demo workspace (or POST /api/demo/workspace).
Regenerate with: cd backend && python -c "from app.demo_data import write_demo_folder; write_demo_folder('../demo_invoices')"

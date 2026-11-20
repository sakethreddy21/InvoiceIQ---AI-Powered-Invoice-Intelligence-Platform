# InvoiceIQ fictional test dataset

All documents in this directory are fictional and intended only for software testing.
They are not valid invoices, tax advice, proof of registration, or documents for payment/tax filing.
All business names, addresses, tax identifiers and amounts are fabricated.

## Files
- DE-001: German domestic invoice with consistent 19% VAT
- DE-002: German invoice with intentionally incorrect VAT arithmetic
- IN-001: Indian intra-state GST invoice with CGST + SGST
- IN-DE-001: Indian supplier to German business, B2B services scenario
- DE-003: Invoice containing reverse-charge wording
- DE-004: Invoice with intentionally omitted supplier/invoice details
- IN-002: Indian inter-state GST invoice with IGST
- DE-005: Credit note with negative amounts

See expected_test_results.json for test expectations.

Important:
- GSTIN strings are fictional and must not be treated as active registrations.
- Cross-border tax treatment requires transaction facts and professional review.
- A format check is not a registration-status verification.
- DATEV compatibility is not tested by these PDFs.

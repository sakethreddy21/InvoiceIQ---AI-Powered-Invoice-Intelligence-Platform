import json
from pathlib import Path

from app.extraction import extract_invoice, extract_pdf_fields
from app.validation import gstin_check_character, validate_invoice

DATASET = Path(__file__).parents[2] / "InvoiceIQ_test_invoices"

def test_supplied_invoice_dataset_extraction_and_expected_rules() -> None:
    dataset = DATASET
    expected = json.loads((dataset / "expected_test_results.json").read_text())

    for filename, scenario in expected.items():
        fields, _ = extract_invoice(dataset / filename, "application/pdf")
        key_amounts = scenario.get("key_amounts", {})
        if "missing" not in scenario["expected"].casefold():
            assert fields.get("invoice_number"), f"{filename}: invoice number was not extracted"
        assert fields.get("net_amount") is not None, f"{filename}: net amount was not extracted"
        assert fields.get("vat_amount") is not None, f"{filename}: tax amount was not extracted"
        assert fields.get("gross_amount") is not None, f"{filename}: gross amount was not extracted"

        for expected_key, field in (
            ("net", "net_amount"),
            ("vat", "vat_amount"),
            ("vat_stated", "vat_amount"),
            ("gross", "gross_amount"),
            ("gross_stated", "gross_amount"),
        ):
            expected_value = key_amounts.get(expected_key)
            if expected_value is not None:
                assert fields[field] == expected_value, f"{filename}: {field} expected {expected_value}, got {fields[field]}"

        results = validate_invoice(fields)
        rule_results = {str(result["rule_id"]): str(result["result"]) for result in results}
        if filename.startswith("DE-002"):
            assert rule_results["DE-VAT-ARITHMETIC"] == "FAIL"
        elif filename.startswith("DE-001") or filename.startswith("DE-005"):
            assert rule_results["DE-VAT-ARITHMETIC"] == "PASS"
        elif filename.startswith("IN-001") or filename.startswith("IN-002"):
            assert rule_results["IN-GST-ARITHMETIC"] == "PASS"

        if filename.startswith("DE-001") or filename.startswith("DE-002"):
            assert rule_results["DE-VAT-RATE"] == "PASS"
            assert rule_results["DE-SERVICE-DATE"] == "PASS"
            assert fields["service_date"] == fields["invoice_date"]
            assert rule_results["INVOICE-SUPPLIER_ADDRESS"] == "PASS"

        if filename.startswith("IN-001"):
            assert fields["supplier_gstin"] == "36ABCDE1234F1Z5"
            assert fields["cgst_rate"] == fields["sgst_rate"] == "9"
            assert rule_results["IN-CGST-SGST-RATES"] == "PASS"
            assert rule_results["IN-GST-SUPPLY-TYPE"] == "PASS"
            # The dataset's GSTINs are fictional, so their check characters do not match.
            assert rule_results["IN-GSTIN-SUPPLIER-CHECKSUM"] == "NEEDS_REVIEW"
            assert rule_results["INVOICE-SUPPLIER_ADDRESS"] == "PASS"
            assert fields["customer_gstin"] == "36BCDEF2345G1Z4"
            assert fields["tax_type"] == "CGST + SGST"
            assert rule_results["IN-GSTIN-SUPPLIER-FORMAT"] == "PASS"
            assert rule_results["IN-GSTIN-CUSTOMER-FORMAT"] == "PASS"
        elif filename.startswith("IN-002"):
            assert fields["customer_gstin"] == "27BCDEF2345G1Z4"
            assert fields["tax_type"] == "IGST"
            assert fields["vat_rate"] == "18"
            assert rule_results["IN-GST-SUPPLY-TYPE"] == "PASS"
        elif filename.startswith("IN-DE"):
            assert fields["transaction_direction"] == "India_to_Germany"
            assert fields["exchange_rate_status"] == "missing"
            assert fields["tax_type"] == "reverse_charge_or_zero_rated_review"
            assert fields["service_date"] == "01–31 Oct 2026"
            assert rule_results["CROSS-BORDER-ADVISOR-REVIEW"] == "NEEDS_REVIEW"
            assert "No exchange rate" in next(
                result["explanation"]
                for result in results
                if result["rule_id"] == "CROSS-BORDER-ADVISOR-REVIEW"
            )
        elif filename.startswith("DE-003"):
            assert fields.get("reverse_charge_wording")
            assert rule_results["REVERSE-CHARGE-CLAIM-REVIEW"] == "NEEDS_REVIEW"
        elif filename.startswith("DE-004"):
            assert "invoice_number" not in fields
            assert rule_results["INVOICE-SUPPLIER_TAX_ID"] == "NEEDS_REVIEW"
            assert rule_results["INVOICE-SUPPLIER_ADDRESS"] == "NEEDS_REVIEW"
            assert rule_results["DE-SERVICE-DATE"] == "NEEDS_REVIEW"
        elif filename.startswith("DE-005"):
            assert fields["document_type"] == "credit_note"
            assert fields["net_amount"] == "-50.00"
            assert fields["reference_invoice_number"] == "DE-001-2026"
            assert rule_results["CREDIT-NOTE-REFERENCE"] == "PASS"
            assert "DE-SERVICE-DATE" not in rule_results


def test_gstin_checksum_uses_mod_36_algorithm() -> None:
    assert gstin_check_character("27AAPFU0939F1ZV") == "V"
    assert gstin_check_character("29AAGCB7383J1Z4") == "4"


def test_gst_supply_type_mismatch_needs_review() -> None:
    fields, _ = extract_invoice(DATASET / "IN-002_indian_gst_igst.pdf", "application/pdf")
    fields["place_of_supply"] = "Telangana (36)"
    result = next(r for r in validate_invoice(fields) if r["rule_id"] == "IN-GST-SUPPLY-TYPE")
    assert result["result"] == "NEEDS_REVIEW"
    assert result["expected_value"] == "CGST + SGST"


def test_non_tax_percentages_are_not_used_as_vat_rate() -> None:
    fields = extract_pdf_fields("Subtotal: 100.00\nDiscount (20%): 20.00\nTotal: 80.00\n")
    assert "vat_rate" not in fields
    assert "tax_type" not in fields


def test_unusual_german_vat_rate_needs_review() -> None:
    fields, _ = extract_invoice(DATASET / "DE-001_valid_german_19pct.pdf", "application/pdf")
    fields["vat_rate"] = "16"
    result = next(r for r in validate_invoice(fields) if r["rule_id"] == "DE-VAT-RATE")
    assert result["result"] == "NEEDS_REVIEW"

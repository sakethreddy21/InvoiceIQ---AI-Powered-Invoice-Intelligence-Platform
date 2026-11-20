from datetime import date
from pathlib import Path

import pymupdf
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import cross_border, fx
from app.datev import build_csv
from app.demo_data import demo_files
from app.extraction import extract_document
from app.migrate import upgrade_database
from app.models import Base, BookingSuggestion, Organization
from conftest import DATASET, upload

ECB_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube><Cube time="2026-11-04"><Cube currency="USD" rate="1.0800"/><Cube currency="INR" rate="95.1000"/><Cube currency="GBP" rate="0.8400"/></Cube>
  <Cube time="2026-10-30"><Cube currency="INR" rate="94.0000"/></Cube></Cube>
</gesmes:Envelope>"""


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "demo"
    folder.mkdir()
    for name, content, _ in demo_files():
        (folder / name).write_bytes(content)
    return folder


def _extract(path: Path) -> dict:
    return extract_document(path, "application/xml" if path.suffix == ".xml" else "application/pdf").fields


# --- Extraction unit tests -----------------------------------------------------------------------

def test_layout_pairs_labels_with_values_in_columns(tmp_path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page()
    for (x, y), text in {
        (40, 40): "Northwind Supplies", (460, 40): "INVOICE", (520, 62): "# 4711",
        (40, 120): "Bill To:", (40, 136): "Example Kunde GmbH",
        (430, 110): "Date:", (510, 110): "Nov 05 2026",
        (420, 330): "Subtotal:", (530, 330): "EUR 200.00",
        (420, 350): "VAT (19%):", (530, 350): "EUR 38.00",
        (420, 370): "Total:", (530, 370): "EUR 238.00",
    }.items():
        page.insert_text((x, y), text)
    path = tmp_path / "columns.pdf"
    document.save(path)
    fields = extract_document(path, "application/pdf").fields
    assert (fields["invoice_number"], fields["invoice_date"], fields["customer_name"]) == ("4711", "2026-11-05", "Example Kunde GmbH")
    assert (fields["net_amount"], fields["vat_amount"], fields["gross_amount"], fields["vat_rate"], fields["currency"]) == ("200.00", "38.00", "238.00", "19", "EUR")


def test_pdf_extraction_of_demo_invoice_with_confidence(demo_dir: Path) -> None:
    result = extract_document(demo_dir / "DEMO-01_valid_german_19pct.pdf", "application/pdf")
    f = result.fields
    assert (f["invoice_number"], f["invoice_date"], f["service_date"], f["due_date"]) == ("DEMO-DE-001", "2026-11-02", "2026-10-30", "2026-11-16")
    assert f["iban"].startswith("DE") and f["bic"] == "COBADEFFXXX" and f["payment_terms"] == "14 Tage"
    assert f["line_items"] == [{"description": "Büromaterial und Druckerzubehör", "quantity": "4", "unit_price": "50.00", "net_amount": "200.00", "vat_amount": "38.00", "vat_rate": "19"}]
    assert result.field_meta["invoice_number"]["source"] == "label" and result.field_meta["invoice_number"]["confidence"] > 0.9
    assert result.field_meta["supplier_name"]["bbox"]


def test_xrechnung_ubl_extraction(demo_dir: Path) -> None:
    result = extract_document(demo_dir / "DEMO-06_duplicate_of_DEMO-01_xrechnung.xml", "application/xml")
    f = result.fields
    assert result.method == "xml" and f["einvoice_format"] == "xrechnung-ubl"
    assert (f["invoice_number"], f["net_amount"], f["vat_amount"], f["gross_amount"], f["vat_rate"]) == ("DEMO-DE-001", "200.00", "38.00", "238.00", "19")
    assert f["supplier_country"] == "Germany" and f["supplier_vat_id"].startswith("DE")
    assert result.field_meta["gross_amount"]["confidence"] == 0.99


def test_zugferd_hybrid_prefers_embedded_xml(demo_dir: Path) -> None:
    result = extract_document(demo_dir / "DEMO-04_germany_to_india_consulting_zugferd.pdf", "application/pdf")
    f = result.fields
    assert result.method == "zugferd" and result.embedded_xml and f["einvoice_format"] == "zugferd-cii"
    assert (f["customer_country"], f["gross_amount"], f["due_date"]) == ("India", "5000.00", "2026-12-05")
    assert "§ 3a Abs. 2 UStG" in f["tax_exemption_reference"]
    assert result.field_meta["gross_amount"]["source"] == "xml"


def test_scanned_pdf_ocr_reads_party_columns(tmp_path: Path) -> None:
    from app.ocr import available_provider

    if available_provider() is None:
        pytest.skip("No OCR engine installed")
    source = pymupdf.open(DATASET / "IN-001_indian_gst_cgst_sgst.pdf")
    scanned = pymupdf.open()
    page = scanned.new_page(width=source[0].rect.width, height=source[0].rect.height)
    page.insert_image(page.rect, pixmap=source[0].get_pixmap(dpi=200))
    path = tmp_path / "scan.pdf"
    scanned.save(path)
    result = extract_document(path, "application/pdf")
    assert result.method == "ocr"
    assert (result.fields["supplier_gstin"], result.fields["customer_gstin"], result.fields["gross_amount"]) == ("36ABCDE1234F1Z5", "36BCDEF2345G1Z4", "59000.00")
    assert result.field_meta["gross_amount"]["confidence"] < 0.92  # OCR lowers confidence


# --- Classification and currency ------------------------------------------------------------------

@pytest.mark.parametrize(("supplier", "customer", "expected"), [
    ("India", "Germany", "India_to_Germany"), ("Germany", "India", "Germany_to_India"),
    ("Germany", "Germany", "domestic_germany"), ("India", "India", "domestic_india"),
    ("Germany", "United States", "other_international"), (None, "Germany", "unknown"),
])
def test_transaction_classification(supplier, customer, expected) -> None:
    assert cross_border.classify({"supplier_country": supplier, "customer_country": customer}) == expected


def test_goods_and_services_are_assessed_separately() -> None:
    values = {"supplier_country": "Germany", "customer_country": "India", "invoice_date": "2026-11-05"}
    services = cross_border.assess(values, {"supply_type": "services", "customer_is_business": "yes"})
    goods = cross_border.assess(values, {"supply_type": "goods", "goods_physically_moved": "yes", "export_evidence_available": "yes"})
    assert {i["rule_id"] for i in services["indicators"]} == {"DE-B2B-SERVICES-TO-THIRD-COUNTRY", "IN-IMPORT-OF-SERVICES-RCM"}
    assert {i["rule_id"] for i in goods["indicators"]} == {"DE-EXPORT-DELIVERY", "IN-IMPORT-OF-GOODS"}
    b2c = cross_border.assess(values, {"supply_type": "services", "customer_is_business": "no"})
    assert [i["rule_id"] for i in b2c["indicators"]] == ["DE-B2C-SERVICES-FROM-GERMANY"]
    assert services["advisor_review_required"] and services["confidence"] == "high"
    assert all(i["reasoning"] and i["references"] for i in services["indicators"])


def test_currency_conversion_records_rate_and_date(monkeypatch: pytest.MonkeyPatch) -> None:
    fx._cache.clear()
    monkeypatch.setattr(fx, "fetch_ecb_xml", lambda url: ECB_SAMPLE)
    for currency, units, converted in (("INR", "95.1000", "1051.52"), ("USD", "1.0800", "92592.59"), ("GBP", "0.8400", "119047.62")):
        rate = fx.ecb_rate(currency, date(2026, 11, 5))
        result = fx.convert({"gross_amount": "100000.00"}, rate)
        assert (result["units_per_eur"], result["rate_date"], result["converted"]["gross_amount"]) == (units, "2026-11-04", converted)
        assert "ECB" in result["source"] and result["is_demo_rate"] is False
    demo = fx.convert({"gross_amount": "900.00"}, fx.demo_rate("INR", date(2026, 11, 5)))
    assert demo["is_demo_rate"] is True and "DEMO RATE" in demo["source"] and demo["converted"]["gross_amount"] == "10.00"
    with pytest.raises(fx.FxError):
        fx.ecb_rate("XYZ", date(2026, 11, 5))


# --- DATEV formatting ---------------------------------------------------------------------------

def test_datev_formatting_conventions() -> None:
    org = Organization(name="X", account_length=4, datev_consultant_number="1001", datev_client_number="1", fiscal_year_start="01-01")
    eur = BookingSuggestion(booking={"amount": "1234.50", "debit_credit": "S", "account": "4930", "contra_account": "70000", "tax_key": "9",
                                     "document_date": "2026-11-02", "document_number": "RE-1", "text": 'Quote "test"', "currency": "EUR",
                                     "original_currency": "EUR", "partner_name": "Nordlicht GmbH", "partner_vat_id": "DE123"})
    inr = BookingSuggestion(booking={"amount": "1051.52", "debit_credit": "S", "account": "4900", "contra_account": "70000", "tax_key": None,
                                     "document_date": "2026-11-04", "document_number": "IN-1", "text": "Deccan", "currency": "EUR",
                                     "original_currency": "INR", "original_gross_amount": "100000.00", "exchange_rate": "95.1"})
    content, filename = build_csv(org, [eur, inr])
    assert filename == "EXTF_Buchungsstapel.csv" and content.endswith("\r\n")
    header, columns, row_eur, row_inr = content.rstrip("\r\n").split("\r\n")
    assert header.startswith('"EXTF";700;21;"Buchungsstapel";13;') and ";1001;1;20260101;4;20261102;20261104;" in header
    assert row_eur.startswith('1234,50;"S";"EUR";;;"";4930;70000;"9";0211;"RE-1";"";;"Quote ""test"""')
    assert '"Geschäftspartner";"Nordlicht GmbH";"USt-IdNr.";"DE123"' in row_eur
    assert row_inr.startswith('100000,00;"S";"INR";95,100000;1051,52;"EUR";4900;70000;"";0411;"IN-1"')
    content.encode("cp1252")


# --- Integration: the six demo invoices through the whole workflow ----------------------------------

def test_demo_workspace_processes_all_six_invoices(client: TestClient, auth: dict[str, str], monkeypatch) -> None:
    fx._cache.clear()
    monkeypatch.setattr(fx, "fetch_ecb_xml", lambda url: (_ for _ in ()).throw(fx.FxError("offline")))
    created = client.post("/api/demo/workspace", headers=auth)
    assert created.status_code == 201, created.text
    org_id = created.json()["organization_id"]
    demo = {**auth, "X-Organization-ID": org_id}
    invoices = {i["filename"][:7]: i for i in client.get("/api/invoices", headers=demo).json()["items"]}
    assert len(invoices) == 6

    def rules(key: str) -> dict[str, str]:
        return {r["rule_id"]: r["result"] for r in invoices[key]["validation_results"]}

    assert invoices["DEMO-01"]["compliance_status"] == "FAIL" and rules("DEMO-01")["DUPLICATE-INVOICE"] == "FAIL"  # resent as XML
    assert rules("DEMO-06")["DUPLICATE-INVOICE"] == "FAIL" and invoices["DEMO-06"]["extraction_method"] == "xml"
    assert rules("DEMO-02")["DE-VAT-ARITHMETIC"] == "FAIL"
    assert rules("DEMO-05")["IN-GSTIN-SUPPLIER-FORMAT"] == "FAIL"
    assert invoices["DEMO-04"]["extraction_method"] == "zugferd" and invoices["DEMO-04"]["direction"] == "outgoing"
    assert invoices["DEMO-03"]["fx_conversion"]["is_demo_rate"] is True  # ECB offline -> labelled demo rate
    assert invoices["DEMO-03"]["fx_conversion"]["converted"]["gross_amount"] == "2777.78"

    # Cross-border assessment for both directions, stored and reviewable.
    for key, direction, expected in (("DEMO-03", "India_to_Germany", "DE-REVERSE-CHARGE-FOREIGN-SERVICES"), ("DEMO-04", "Germany_to_India", "IN-IMPORT-OF-SERVICES-RCM")):
        assessment = client.post(f"/api/invoices/{invoices[key]['id']}/cross-border", headers=demo)
        assert assessment.status_code == 201
        body = assessment.json()
        assert body["direction"] == direction and body["status"] == "ready_for_advisor_review"
        assert expected in {i["rule_id"] for i in body["indicators"]} and body["recommended_actions"]
        reviewed = client.post(f"/api/invoices/{invoices[key]['id']}/cross-border/{body['saved_assessment']['id']}/review",
                               json={"status": "reviewed", "note": "Confirmed with advisor"}, headers=demo)
        assert reviewed.json()["advisor_status"] == "reviewed"
    gstin = client.get(f"/api/invoices/{invoices['DEMO-03']['id']}/cross-border", headers=demo).json()["parties"]["supplier"]["gstin"]
    assert gstin["structurally_valid"] and gstin["checksum_valid"] and gstin["state_name"] == "Telangana"

    # Accounting: suggestions where checks allow, blocked with a reason where they do not.
    outcomes = {key: client.post(f"/api/invoices/{invoices[key]['id']}/accounting", headers=demo) for key in invoices}
    assert {k for k, r in outcomes.items() if r.status_code == 201} == {"DEMO-03", "DEMO-04"}
    for key in ("DEMO-01", "DEMO-02", "DEMO-05", "DEMO-06"):
        assert outcomes[key].status_code == 422 and "Resolve failed checks" in outcomes[key].json()["detail"]
    sale = outcomes["DEMO-04"].json()["booking"]
    assert (sale["direction"], sale["account"], sale["contra_account"], sale["gross_amount"]) == ("outgoing", "10000", "8338", "5000.00")
    purchase = outcomes["DEMO-03"].json()["booking"]
    assert (purchase["account"], purchase["original_currency"], purchase["exchange_rate"]) == ("4900", "INR", "90.0000")
    assert "§13b" in purchase["tax_treatment"]

    # Fix the duplicate and VAT defects like a reviewer would, then book and export.
    client.delete(f"/api/invoices/{invoices['DEMO-06']['id']}", headers=demo)
    rechecked = client.get(f"/api/invoices/{invoices['DEMO-01']['id']}", headers=demo).json()
    assert rechecked["compliance_status"] == "PASS"  # deleting the duplicate re-checks its counterpart
    booking = client.post(f"/api/invoices/{invoices['DEMO-01']['id']}/accounting", headers=demo).json()["booking"]
    assert (booking["account"], booking["tax_key"], booking["net_amount"], booking["vat_amount"]) == ("4930", "9", "200.00", "38.00")
    for key in ("DEMO-01", "DEMO-03", "DEMO-04"):
        assert client.post(f"/api/invoices/{invoices[key]['id']}/approve", headers=demo).json()["status"] == "approved"
    preview = client.get("/api/exports/datev/preview", headers=demo).json()
    assert preview["exportable"] == 3, [r["errors"] for r in preview["rows"]]
    assert preview["disclaimer"].startswith("DATEV-ready prototype")
    batch = client.post("/api/export/datev", headers=demo)
    assert batch.status_code == 201
    download = client.get(f"/api/export/{batch.json()['id']}/download", headers=demo)
    assert 'filename="EXTF_Buchungsstapel.csv"' in download.headers["content-disposition"]
    lines = download.content.decode("cp1252").strip().split("\r\n")
    assert len(lines) == 5 and any(line.startswith('250000,00;"S";"INR";90,000000;2777,78;"EUR";4900;70000') for line in lines)
    stats = client.get("/api/dashboard/stats", headers=demo).json()
    assert stats["invoices_processed"] == 5 and stats["by_direction"]["India_to_Germany"] == 1


def test_inbox_filters(client: TestClient, auth: dict[str, str]) -> None:
    upload(client, auth, "DE-001_valid_german_19pct.pdf", "IN-002_indian_gst_igst.pdf", "DE-002_incorrect_vat_math.pdf")
    get = lambda query: [i["invoice_number"] for i in client.get(f"/api/invoices?{query}", headers=auth).json()["items"]]  # noqa: E731
    assert get("currency=INR") == ["IN-002-2026"]
    assert get("compliance=FAIL") == ["DE-002-2026"]
    assert sorted(get("date_from=2026-11-06")) == ["DE-001-2026", "DE-002-2026"]
    assert get("date_to=2026-11-02") == ["IN-002-2026"]
    assert get("search=weststadt") == ["DE-002-2026"]
    assert get("direction=domestic_india") == ["IN-002-2026"]


def test_email_ingestion_is_not_claimed_functional(client: TestClient, auth: dict[str, str]) -> None:
    body = client.get("/api/ingestion/email", headers=auth).json()
    assert body["functional"] is False and body["status"] == "not_configured"


def test_retention_purge_and_tax_rules(client: TestClient, auth: dict[str, str]) -> None:
    upload(client, auth, "DE-001_valid_german_19pct.pdf")
    assert client.post("/api/organization/retention/purge", headers=auth).json() == {"deleted": 0}
    rules = client.get("/api/tax-rules", headers=auth).json()
    assert {"DE-REVERSE-CHARGE-FOREIGN-SERVICES", "IN-EXPORT-OF-SERVICES"} <= {r["rule_id"] for r in rules}
    assert all(r["version"] and r["valid_from"] for r in rules)


def test_foreign_currency_booking_requires_conversion(client: TestClient, auth: dict[str, str]) -> None:
    invoice = upload(client, auth, "IN-DE-001_india_to_germany_services.pdf")[0]
    client.post("/api/accounting/mappings", json={"name": "Data", "keywords": ["analytics"], "expense_account": "4900", "creditor_account": "70000"}, headers=auth)
    client.patch(f"/api/invoices/{invoice['id']}", json={"fields": {"customer_vat_id": None, "customer_tax_id": None}}, headers=auth)
    response = client.post(f"/api/invoices/{invoice['id']}/accounting", headers=auth)
    assert response.status_code == 422 and "Convert this INR invoice" in response.json()["detail"]


def test_migrations_build_the_model_schema_on_sqlite(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    upgrade_database(engine)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_demo_workspace_reset_restores_initial_state(client: TestClient, auth: dict[str, str], monkeypatch) -> None:
    fx._cache.clear()
    monkeypatch.setattr(fx, "fetch_ecb_xml", lambda url: (_ for _ in ()).throw(fx.FxError("offline")))
    org_id = client.post("/api/demo/workspace", headers=auth).json()["organization_id"]
    demo = {**auth, "X-Organization-ID": org_id}
    assert client.get("/api/demo/workspace", headers=demo).json() == {"is_demo": True}
    assert client.get("/api/demo/workspace", headers=auth).json() == {"is_demo": False}

    # Use the workspace like a presenter would: delete, book, approve, export.
    invoices = {i["filename"][:7]: i for i in client.get("/api/invoices", headers=demo).json()["items"]}
    client.delete(f"/api/invoices/{invoices['DEMO-06']['id']}", headers=demo)
    client.post(f"/api/invoices/{invoices['DEMO-03']['id']}/accounting", headers=demo)
    client.post(f"/api/invoices/{invoices['DEMO-03']['id']}/approve", headers=demo)
    assert client.post("/api/export/datev", headers=demo).status_code == 201

    reset = client.post("/api/demo/workspace/reset", headers=demo)
    assert reset.status_code == 200, reset.text
    assert reset.json()["organization_id"] == org_id and len(reset.json()["invoices"]) == 6
    after = client.get("/api/invoices", headers=demo).json()["items"]
    assert len(after) == 6 and all(i["status"] != "approved" for i in after)
    assert client.get("/api/accounting/suggestions", headers=demo).json() == []
    assert client.get("/api/exports/datev", headers=demo).json() == []
    duplicate = next(i for i in after if i["filename"].startswith("DEMO-01"))
    assert {r["rule_id"]: r["result"] for r in duplicate["validation_results"]}["DUPLICATE-INVOICE"] == "FAIL"

    # Creating the demo again reuses the same workspace instead of adding another organization.
    again = client.post("/api/demo/workspace", headers=auth).json()
    assert again["organization_id"] == org_id and again["reused"] is True
    assert len(client.get("/api/auth/me", headers=auth).json()["memberships"]) == 2

    # Real workspaces can never be reset.
    refused = client.post("/api/demo/workspace/reset", headers=auth)
    assert refused.status_code == 400

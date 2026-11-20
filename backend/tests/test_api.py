import pymupdf
from fastapi.testclient import TestClient

from app.settings import settings
from conftest import DATASET, PASSWORD, Register, upload


def test_health_reports_service_version(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok", "service": "InvoiceIQ API", "version": "0.3.0"}


def test_readiness_checks_database_connection(client: TestClient) -> None:
    assert client.get("/api/ready").json() == {"status": "ready", "database": "connected"}


def test_endpoints_require_sign_in(client: TestClient) -> None:
    for method, path in (("get", "/api/invoices"), ("get", "/api/dashboard/stats"), ("post", "/api/invoices/x/validate")):
        assert getattr(client, method)(path).status_code == 401
    assert client.get("/api/invoices", headers={"Authorization": "Bearer forged.token"}).status_code == 401


def test_register_login_and_session(client: TestClient, register: Register) -> None:
    register()
    assert client.post("/api/auth/register", json={
        "email": "ADMIN@example.com", "password": PASSWORD, "name": "x", "organization_name": "y"
    }).status_code == 409
    assert client.post("/api/auth/login", json={"email": "admin@example.com", "password": "wrong-password"}).status_code == 401
    login = client.post("/api/auth/login", json={"email": "Admin@Example.com", "password": PASSWORD})
    assert login.status_code == 200
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}).json()
    assert me["role"] == "admin" and me["organization"]["name"] == "Northstar Finance"


def test_repeated_failed_logins_are_throttled(client: TestClient, register: Register) -> None:
    register()
    for _ in range(10):
        client.post("/api/auth/login", json={"email": "admin@example.com", "password": "wrong-password"})
    response = client.post("/api/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    assert response.status_code == 429


def test_organizations_are_isolated(client: TestClient, register: Register) -> None:
    org_a = register("a@example.com", "Org A")
    org_b = register("b@example.com", "Org B")
    invoice = upload(client, org_a, "DE-001_valid_german_19pct.pdf")[0]
    assert client.get("/api/invoices", headers=org_b).json()["total"] == 0
    for method, path in (
        ("get", f"/api/invoices/{invoice['id']}"),
        ("get", f"/api/invoices/{invoice['id']}/file-url"),
        ("patch", f"/api/invoices/{invoice['id']}"),
        ("post", f"/api/invoices/{invoice['id']}/validate"),
        ("delete", f"/api/invoices/{invoice['id']}"),
    ):
        kwargs = {"json": {"fields": {}}} if method == "patch" else {}
        assert getattr(client, method)(path, headers=org_b, **kwargs).status_code == 404, path
    # The same document may exist independently in another organization.
    assert upload(client, org_b, "DE-001_valid_german_19pct.pdf")[0]["id"] != invoice["id"]


def test_roles_limit_actions(client: TestClient, auth: dict[str, str]) -> None:
    created = client.post(
        "/api/organization/members",
        json={"email": "viewer@example.com", "name": "Viewer", "role": "viewer", "password": PASSWORD},
        headers=auth,
    )
    assert created.status_code == 201
    token = client.post("/api/auth/login", json={"email": "viewer@example.com", "password": PASSWORD}).json()["access_token"]
    viewer = {"Authorization": f"Bearer {token}"}
    pdf = (DATASET / "DE-001_valid_german_19pct.pdf").read_bytes()
    assert client.post("/api/invoices/upload", files=[("files", ("a.pdf", pdf, "application/pdf"))], headers=viewer).status_code == 403
    assert client.patch("/api/organization", json={"name": "x"}, headers=viewer).status_code == 403
    assert client.get("/api/invoices", headers=viewer).status_code == 200
    me = client.get("/api/auth/me", headers=auth).json()
    assert client.patch(f"/api/organization/members/{me['user']['id']}", json={"role": "viewer"}, headers=auth).status_code == 409


def test_upload_deduplicate_edit_validate(client: TestClient, auth: dict[str, str]) -> None:
    invoice = upload(client, auth, "DE-002_incorrect_vat_math.pdf")[0]
    assert invoice["extraction_method"] == "text"
    assert upload(client, auth, "DE-002_incorrect_vat_math.pdf")[0]["id"] == invoice["id"]
    url = f"/api/invoices/{invoice['id']}"

    # Checks run automatically on upload.
    assert invoice["compliance_status"] == "FAIL" and invoice["status"] == "validation_failed"
    failed = client.post(f"{url}/validate", headers=auth).json()
    assert failed["overall_status"] == "FAIL"
    assert next(r for r in failed["results"] if r["rule_id"] == "DE-VAT-ARITHMETIC")["expected_value"] == "190.00"

    corrected = client.patch(url, json={"fields": {"vat_amount": "190.00", "gross_amount": "1190.00"}}, headers=auth).json()
    assert corrected["original_data"]["vat_amount"] == "180.00"
    assert corrected["effective_data"]["vat_amount"] == "190.00"
    # Validation re-runs whenever fields change; the fictional VAT ID's check digit still needs review.
    results = {r["rule_id"]: r["result"] for r in corrected["validation_results"]}
    assert results["DE-VAT-ARITHMETIC"] == "PASS" and results["VAT-ID-SUPPLIER-CHECKSUM"] == "NEEDS_REVIEW"
    assert corrected["compliance_status"] == "NEEDS_REVIEW"
    assert any(e["action"] == "fields_corrected" and e["actor"] == "admin@example.com" for e in corrected["audit_history"])
    audit = client.get(f"{url}/audit", headers=auth).json()
    assert [e["action"] for e in audit][:3] == ["uploaded", "extracted", "checks_run"]

    reverted = client.patch(url, json={"fields": {"vat_amount": "180.00", "gross_amount": "1180.00"}}, headers=auth).json()
    assert reverted["corrected_data"] == {}


def test_file_preview_uses_short_lived_signed_link(client: TestClient, auth: dict[str, str]) -> None:
    invoice = upload(client, auth, "DE-001_valid_german_19pct.pdf")[0]
    url = client.get(f"/api/invoices/{invoice['id']}/file-url", headers=auth).json()["url"]
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == (DATASET / "DE-001_valid_german_19pct.pdf").read_bytes()
    assert client.get(url[:-3] + "abc").status_code == 404
    assert client.get(f"/api/invoices/{invoice['id']}/file").status_code == 404
    assert client.get(f"/api/invoices/{invoice['id']}/file", headers=auth).status_code == 200
    page = client.get(f"/api/invoices/{invoice['id']}/pages/0?token={url.split('token=')[1]}")
    assert page.status_code == 200 and page.content.startswith(b"\x89PNG")
    assert invoice["pages"][0]["width"] > 0
    assert invoice["field_meta"]["gross_amount"]["bbox"] and invoice["field_meta"]["gross_amount"]["confidence"] >= 0.8


def test_upload_rejects_mismatched_file_extension(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post("/api/invoices/upload", files=[("files", ("invoice.pdf", b"not a PDF", "application/pdf"))], headers=auth)
    assert response.status_code == 415


def test_xml_native_extraction(client: TestClient, auth: dict[str, str]) -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Invoice xmlns="urn:example:invoice"><ID>XML-2026-01</ID><IssueDate>2026-11-08</IssueDate>
      <DocumentCurrencyCode>EUR</DocumentCurrencyCode><TaxExclusiveAmount>200.00</TaxExclusiveAmount>
      <TaxAmount>38.00</TaxAmount><TaxInclusiveAmount>238.00</TaxInclusiveAmount></Invoice>"""
    response = client.post("/api/invoices/upload", files=[("files", ("invoice.xml", xml, "application/xml"))], headers=auth)
    data = response.json()["files"][0]["invoice"]
    assert data["extraction_method"] == "xml"
    assert data["original_data"]["invoice_number"] == "XML-2026-01"
    assert data["original_data"]["vat_amount"] == "38.00"


def test_scanned_pdf_without_ocr_is_reported_not_guessed(client: TestClient, auth: dict[str, str], monkeypatch) -> None:
    monkeypatch.setattr("app.ocr.available_provider", lambda: None)
    source = pymupdf.open(DATASET / "DE-001_valid_german_19pct.pdf")
    scanned = pymupdf.open()
    page = scanned.new_page(width=source[0].rect.width, height=source[0].rect.height)
    page.insert_image(page.rect, pixmap=source[0].get_pixmap(dpi=150))
    content = scanned.tobytes()
    response = client.post("/api/invoices/upload", files=[("files", ("scan.pdf", content, "application/pdf"))], headers=auth)
    queued = response.json()["files"][0]["invoice"]
    assert queued["status"] == "processing"  # scanned PDFs are processed in the background
    invoice = client.get(f"/api/invoices/{queued['id']}", headers=auth).json()
    assert invoice["extraction_method"] == "ocr_unavailable"
    assert not any(key.endswith("_amount") for key in invoice["original_data"])
    extracted = next(e for e in invoice["audit_history"] if e["action"] == "extracted")
    assert "no OCR engine is available" in extracted["warnings"][0]


def test_dashboard_delete_and_missing_file(client: TestClient, auth: dict[str, str]) -> None:
    ids = [i["id"] for i in upload(client, auth, "DE-001_valid_german_19pct.pdf", "IN-DE-001_india_to_germany_services.pdf")]
    stats = client.get("/api/dashboard/stats", headers=auth).json()
    assert (stats["invoices_processed"], stats["awaiting_review"], stats["cross_border_flagged"]) == (2, 2, 1)
    assert stats["by_direction"] == {"domestic_germany": 1, "India_to_Germany": 1}
    assert client.delete(f"/api/invoices/{ids[0]}", headers=auth).status_code == 204
    assert client.get(f"/api/invoices/{ids[0]}", headers=auth).status_code == 404
    from pathlib import Path

    assert not list(Path(settings.upload_dir).glob(f"{ids[0]}*"))
    for path in Path(settings.upload_dir).glob(f"{ids[1]}*"):
        path.unlink()
    assert client.post(f"/api/invoices/{ids[1]}/extract", headers=auth).status_code == 404

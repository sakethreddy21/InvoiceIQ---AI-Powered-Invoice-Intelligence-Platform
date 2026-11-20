"""Native extraction for structured e-invoices: XRechnung/UBL, CII and ZUGFeRD/Factur-X.

Structured data is authoritative, so these values get the highest confidence and are
preferred over anything read from the PDF rendering.
"""

from decimal import Decimal, InvalidOperation

from lxml import etree

XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, recover=False, huge_tree=False)
EMBEDDED_XML_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "zugferd.xml", "xrechnung.xml", "ZUGFeRD-invoice.xml")
CREDIT_NOTE_TYPE_CODES = {"381", "396", "532"}
COUNTRY_NAMES = {"DE": "Germany", "IN": "India"}


def parse_xml_bytes(content: bytes) -> etree._Element:
    return etree.fromstring(content, XML_PARSER)


def _children(element: etree._Element | None, *path: str) -> list[etree._Element]:
    """Follow a path of local names (namespace-agnostic)."""
    current = [element] if element is not None else []
    for name in path:
        current = [child for parent in current for child in parent if isinstance(child.tag, str) and etree.QName(child).localname == name]
    return current


def _first(element: etree._Element | None, *path: str) -> etree._Element | None:
    found = _children(element, *path)
    return found[0] if found else None


def _text(element: etree._Element | None, *path: str) -> str | None:
    node = _first(element, *path) if path else element
    value = (node.text or "").strip() if node is not None else ""
    return value or None


def _amount(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(Decimal(value).quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


def _rate(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(Decimal(value).normalize()) if Decimal(value) % 1 else str(int(Decimal(value)))
    except InvalidOperation:
        return None


def _cii_date(node: etree._Element | None) -> str | None:
    value = _text(node)
    if value and len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


UBL_NAMESPACES = {
    "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
}
CII_NAMESPACE = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"


def detect_format(root: etree._Element) -> str | None:
    qname = etree.QName(root)
    name = qname.localname
    if name in ("Invoice", "CreditNote") and qname.namespace in UBL_NAMESPACES:
        customization = (_text(root, "CustomizationID") or "").lower()
        return "xrechnung-ubl" if "xrechnung" in customization else "ubl"
    if name == "CrossIndustryInvoice" and qname.namespace == CII_NAMESPACE:
        guideline = (_text(root, "ExchangedDocumentContext", "GuidelineSpecifiedDocumentContextParameter", "ID") or "").lower()
        if "xrechnung" in guideline:
            return "xrechnung-cii"
        return "zugferd-cii" if any(key in guideline for key in ("zugferd", "factur-x", "en16931", "cen.eu")) else "cii"
    return None


def extract_structured(root: etree._Element) -> tuple[dict[str, object], str] | None:
    """Return (fields, format) for UBL or CII documents, or None for other XML."""
    fmt = detect_format(root)
    if fmt is None:
        return None
    fields = _ubl(root) if fmt in ("xrechnung-ubl", "ubl") else _cii(root)
    fields["einvoice_format"] = fmt
    return {key: value for key, value in fields.items() if value not in (None, "", [])}, fmt


def _set_country(fields: dict[str, object], prefix: str, code: str | None) -> None:
    if code:
        fields[f"{prefix}_country"] = COUNTRY_NAMES.get(code.upper(), code.upper())


def _ubl_party(fields: dict[str, object], party: etree._Element | None, prefix: str) -> None:
    if party is None:
        return
    fields[f"{prefix}_name"] = _text(party, "PartyLegalEntity", "RegistrationName") or _text(party, "PartyName", "Name")
    address = _first(party, "PostalAddress")
    lines = [
        " ".join(filter(None, [_text(address, "StreetName"), _text(address, "BuildingNumber")])),
        _text(address, "AdditionalStreetName"),
        " ".join(filter(None, [_text(address, "PostalZone"), _text(address, "CityName")])),
        _text(address, "Country", "IdentificationCode"),
    ]
    fields[f"{prefix}_address"] = ", ".join(line for line in lines if line) or None
    _set_country(fields, prefix, _text(address, "Country", "IdentificationCode"))
    for scheme in _children(party, "PartyTaxScheme"):
        company_id = _text(scheme, "CompanyID")
        scheme_id = (_text(scheme, "TaxScheme", "ID") or "").upper()
        if company_id and scheme_id in ("VAT", ""):
            fields[f"{prefix}_vat_id"] = company_id
            fields.setdefault(f"{prefix}_tax_id", company_id)
        elif company_id:
            fields.setdefault(f"{prefix}_tax_number", company_id)
            fields.setdefault(f"{prefix}_tax_id", company_id)
    legal_id = _text(party, "PartyLegalEntity", "CompanyID")
    if legal_id and len(legal_id) == 15 and legal_id[:2].isdigit():
        fields[f"{prefix}_gstin"] = legal_id


def _ubl(root: etree._Element) -> dict[str, object]:
    is_credit = etree.QName(root).localname == "CreditNote"
    fields: dict[str, object] = {
        "invoice_number": _text(root, "ID"),
        "invoice_date": _text(root, "IssueDate"),
        "due_date": _text(root, "DueDate") or _text(root, "PaymentMeans", "PaymentDueDate"),
        "currency": (_text(root, "DocumentCurrencyCode") or "").upper() or None,
        "document_type": "credit_note" if is_credit or _text(root, "InvoiceTypeCode") in CREDIT_NOTE_TYPE_CODES else "invoice",
        "service_date": _text(root, "Delivery", "ActualDeliveryDate")
        or " – ".join(filter(None, [_text(root, "InvoicePeriod", "StartDate"), _text(root, "InvoicePeriod", "EndDate")]))
        or None,
        "reference_invoice_number": _text(root, "BillingReference", "InvoiceDocumentReference", "ID"),
        "iban": _text(root, "PaymentMeans", "PayeeFinancialAccount", "ID"),
        "bic": _text(root, "PaymentMeans", "PayeeFinancialAccount", "FinancialInstitutionBranch", "ID"),
        "payment_terms": _text(root, "PaymentTerms", "Note"),
    }
    _ubl_party(fields, _first(root, "AccountingSupplierParty", "Party"), "supplier")
    _ubl_party(fields, _first(root, "AccountingCustomerParty", "Party"), "customer")
    totals = _first(root, "LegalMonetaryTotal")
    fields["net_amount"] = _amount(_text(totals, "TaxExclusiveAmount") or _text(totals, "LineExtensionAmount"))
    fields["gross_amount"] = _amount(_text(totals, "TaxInclusiveAmount") or _text(totals, "PayableAmount"))
    tax_total = next((t for t in _children(root, "TaxTotal") if _first(t, "TaxSubtotal") is not None), _first(root, "TaxTotal"))
    fields["vat_amount"] = _amount(_text(tax_total, "TaxAmount"))
    subtotals = _children(tax_total, "TaxSubtotal")
    _tax_details(
        fields,
        [
            {
                "tax_type": "VAT",
                "category": _text(sub, "TaxCategory", "ID"),
                "rate": _rate(_text(sub, "TaxCategory", "Percent")),
                "taxable_amount": _amount(_text(sub, "TaxableAmount")),
                "tax_amount": _amount(_text(sub, "TaxAmount")),
                "exemption_reason": _text(sub, "TaxCategory", "TaxExemptionReason") or _text(sub, "TaxCategory", "TaxExemptionReasonCode"),
            }
            for sub in subtotals
        ],
    )
    line_tag = "CreditNoteLine" if is_credit else "InvoiceLine"
    fields["line_items"] = [
        {
            "description": _text(line, "Item", "Name") or _text(line, "Item", "Description"),
            "quantity": _text(line, "InvoicedQuantity") or _text(line, "CreditedQuantity"),
            "unit_price": _amount(_text(line, "Price", "PriceAmount")),
            "net_amount": _amount(_text(line, "LineExtensionAmount")),
            "vat_rate": _rate(_text(line, "Item", "ClassifiedTaxCategory", "Percent")),
        }
        for line in _children(root, line_tag)
    ]
    fields["description"] = next((item["description"] for item in fields["line_items"] if item["description"]), None)
    return fields


def _cii_party(fields: dict[str, object], party: etree._Element | None, prefix: str) -> None:
    if party is None:
        return
    fields[f"{prefix}_name"] = _text(party, "Name")
    address = _first(party, "PostalTradeAddress")
    lines = [
        _text(address, "LineOne"),
        _text(address, "LineTwo"),
        " ".join(filter(None, [_text(address, "PostcodeCode"), _text(address, "CityName")])),
        _text(address, "CountryID"),
    ]
    fields[f"{prefix}_address"] = ", ".join(line for line in lines if line) or None
    _set_country(fields, prefix, _text(address, "CountryID"))
    for registration in _children(party, "SpecifiedTaxRegistration"):
        node = _first(registration, "ID")
        value = _text(node)
        scheme = (node.get("schemeID") if node is not None else "") or ""
        if not value:
            continue
        if scheme.upper() == "VA":
            fields[f"{prefix}_vat_id"] = value
            fields.setdefault(f"{prefix}_tax_id", value)
        else:
            fields[f"{prefix}_tax_number"] = value
            fields.setdefault(f"{prefix}_tax_id", value)
        if len(value) == 15 and value[:2].isdigit():
            fields[f"{prefix}_gstin"] = value


def _cii(root: etree._Element) -> dict[str, object]:
    document = _first(root, "ExchangedDocument")
    transaction = _first(root, "SupplyChainTradeTransaction")
    agreement = _first(transaction, "ApplicableHeaderTradeAgreement")
    delivery = _first(transaction, "ApplicableHeaderTradeDelivery")
    settlement = _first(transaction, "ApplicableHeaderTradeSettlement")
    summation = _first(settlement, "SpecifiedTradeSettlementHeaderMonetarySummation")
    means = _first(settlement, "SpecifiedTradeSettlementPaymentMeans")
    terms = _first(settlement, "SpecifiedTradePaymentTerms")
    fields: dict[str, object] = {
        "invoice_number": _text(document, "ID"),
        "invoice_date": _cii_date(_first(document, "IssueDateTime", "DateTimeString")),
        "document_type": "credit_note" if _text(document, "TypeCode") in CREDIT_NOTE_TYPE_CODES else "invoice",
        "currency": (_text(settlement, "InvoiceCurrencyCode") or "").upper() or None,
        "service_date": _cii_date(_first(delivery, "ActualDeliverySupplyChainEvent", "OccurrenceDateTime", "DateTimeString")),
        "due_date": _cii_date(_first(terms, "DueDateDateTime", "DateTimeString")),
        "payment_terms": _text(terms, "Description"),
        "iban": _text(means, "PayeePartyCreditorFinancialAccount", "IBANID"),
        "bic": _text(means, "PayeeSpecifiedCreditorFinancialInstitution", "BICID"),
        "reference_invoice_number": _text(settlement, "InvoiceReferencedDocument", "IssuerAssignedID"),
        "net_amount": _amount(_text(summation, "TaxBasisTotalAmount") or _text(summation, "LineTotalAmount")),
        "vat_amount": _amount(_text(summation, "TaxTotalAmount")),
        "gross_amount": _amount(_text(summation, "GrandTotalAmount")),
    }
    _cii_party(fields, _first(agreement, "SellerTradeParty"), "supplier")
    _cii_party(fields, _first(agreement, "BuyerTradeParty"), "customer")
    _tax_details(
        fields,
        [
            {
                "tax_type": "VAT",
                "category": _text(tax, "CategoryCode"),
                "rate": _rate(_text(tax, "RateApplicablePercent")),
                "taxable_amount": _amount(_text(tax, "BasisAmount")),
                "tax_amount": _amount(_text(tax, "CalculatedAmount")),
                "exemption_reason": _text(tax, "ExemptionReason") or _text(tax, "ExemptionReasonCode"),
            }
            for tax in _children(settlement, "ApplicableTradeTax")
        ],
    )
    fields["line_items"] = [
        {
            "description": _text(line, "SpecifiedTradeProduct", "Name"),
            "quantity": _text(line, "SpecifiedLineTradeDelivery", "BilledQuantity"),
            "unit_price": _amount(_text(line, "SpecifiedLineTradeAgreement", "NetPriceProductTradePrice", "ChargeAmount")),
            "net_amount": _amount(_text(line, "SpecifiedLineTradeSettlement", "SpecifiedTradeSettlementLineMonetarySummation", "LineTotalAmount")),
            "vat_rate": _rate(_text(line, "SpecifiedLineTradeSettlement", "ApplicableTradeTax", "RateApplicablePercent")),
        }
        for line in _children(transaction, "IncludedSupplyChainTradeLineItem")
    ]
    fields["description"] = next((item["description"] for item in fields["line_items"] if item["description"]), None)
    return fields


def _tax_details(fields: dict[str, object], details: list[dict[str, object]]) -> None:
    details = [detail for detail in details if detail.get("rate") is not None or detail.get("tax_amount") is not None]
    fields["tax_details"] = details
    if details:
        fields["vat_rate"] = details[0].get("rate")
        fields["tax_type"] = "VAT"
        categories = {str(detail.get("category") or "") for detail in details}
        if "AE" in categories:
            fields["reverse_charge_wording"] = "VAT category AE (reverse charge)"
            fields["tax_type"] = "reverse_charge_or_zero_rated_review"
        reasons = [str(detail["exemption_reason"]) for detail in details if detail.get("exemption_reason")]
        if reasons:
            fields["tax_exemption_reference"] = "; ".join(reasons)

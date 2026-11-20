"""Generates the six fictional InvoiceIQ demo invoices (PDF, ZUGFeRD hybrid PDF, XRechnung XML).

All names, addresses, tax identifiers, bank details and amounts are FICTIONAL. Identifiers are
generated to pass their checksums (except the intentionally malformed GSTIN) so that only the
intended defects are flagged.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from xml.sax.saxutils import escape

import pymupdf

from app.tax_ids import german_vat_checksum_ok, gstin_check_character

FICTIONAL_NOTICE = "FICTIONAL DEMO DOCUMENT - NOT VALID FOR PAYMENT OR TAX FILING"


def _german_vat_id(seed: int) -> str:
    number = seed
    while not german_vat_checksum_ok(f"{number:09d}"):
        number += 1
    return f"DE{number:09d}"


def _gstin(state: str, pan: str, entity: str = "1") -> str:
    body = f"{state}{pan}{entity}Z"
    return body + gstin_check_character(body)


def _german_iban(blz: str, account: str) -> str:
    bban = f"{blz}{account:0>10}"
    check = 98 - int(bban + "131400") % 97
    return f"DE{check:02d}{bban}"


SELF_NAME = "Rheinblick Analytics GmbH"
SELF_VAT = _german_vat_id(271_838_100)
NORDLICHT_VAT = _german_vat_id(813_402_550)
WESTSTADT_VAT = _german_vat_id(146_920_330)
DECCAN_GSTIN = _gstin("36", "AAFCD4821K")
LOTUS_GSTIN = _gstin("27", "AABCL7392M")
BANYAN_GSTIN = _gstin("29", "AAGCB5517P")
MALFORMED_GSTIN = "29AAKCK1234Q1X"  # 14 characters and no 'Z' in position 14: intentionally malformed
SELF_ADDRESS = ["Theaterstraße 8", "52062 Aachen, Deutschland"]


@dataclass
class DemoInvoice:
    filename: str
    title: str
    number: str
    supplier: list[str]
    customer: list[str]
    meta: list[tuple[str, str]]
    items: list[tuple[str, str, str, str, str, str]]  # description, qty, unit, net, vat label, vat amount
    totals: tuple[str, str, str]
    currency_prefix: str
    notes: list[str] = field(default_factory=list)
    payment: list[str] = field(default_factory=list)


def _draw(invoice: DemoInvoice) -> pymupdf.Document:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    write = lambda x, y, text, size=9: page.insert_text((x, y), text, fontsize=size, fontname="helv")  # noqa: E731
    write(456, 70, "INVOICE", 22)
    write(57, 100, invoice.title, 13)
    write(63, 140, "SUPPLIER", 10)
    write(304, 140, "CUSTOMER", 10)
    for index, line in enumerate(invoice.supplier):
        write(63, 155 + index * 10, line)
    for index, line in enumerate(invoice.customer):
        write(304, 155 + index * 10, line)
    y = 215
    for label, value in invoice.meta:
        write(63, y, label)
        write(182, y, value)
        y += 16
    y += 18
    columns = (60, 230, 262, 340, 420, 500)
    for x, header in zip(columns, ("Description", "Qty", "Unit price", "Net", "VAT", "VAT amount")):
        write(x, y, header, 8)
    y += 16
    for description, qty, unit, net, vat_label, vat_amount in invoice.items:
        values = (description, qty, f"{invoice.currency_prefix} {unit}", f"{invoice.currency_prefix} {net}", vat_label, f"{invoice.currency_prefix} {vat_amount}")
        for x, value in zip(columns, values):
            write(x, y, value, 8)
        y += 14
    y += 20
    for label, value in zip(("Net total", "VAT total", "Gross total"), invoice.totals):
        write(62, y, label)
        write(431, y, f"{invoice.currency_prefix} {value}")
        y += 16
    y += 12
    for line in invoice.payment:
        write(57, y, line)
        y += 12
    if invoice.notes:
        y += 8
        write(57, y, "Notes", 10)
        y += 13
        for line in invoice.notes:
            write(57, y, line, 8)
            y += 11
    write(57, y + 14, FICTIONAL_NOTICE, 8)
    document.set_metadata({"title": f"{invoice.number} (fictional demo)", "author": "InvoiceIQ demo generator"})
    return document


def _pdf(invoice: DemoInvoice) -> bytes:
    document = _draw(invoice)
    content = document.tobytes(garbage=3, deflate=True)
    document.close()
    return content


DE_001 = DemoInvoice(
    "DEMO-01_valid_german_19pct.pdf", "Rechnung DEMO-DE-001", "DEMO-DE-001",
    ["Nordlicht Bürobedarf GmbH", "Musterstraße 12", "52062 Aachen, Deutschland", f"USt-IdNr.: {NORDLICHT_VAT}"],
    [SELF_NAME, *SELF_ADDRESS, f"USt-IdNr.: {SELF_VAT}"],
    [("Rechnungsnummer", "DEMO-DE-001"), ("Rechnungsdatum", "02.11.2026"), ("Leistungsdatum", "30.10.2026"),
     ("Fällig am", "16.11.2026"), ("Währung", "EUR"), ("Zahlungsziel", "14 Tage")],
    [("Büromaterial und Druckerzubehör", "4", "50.00", "200.00", "19%", "38.00")],
    ("200.00", "38.00", "238.00"), "EUR",
    payment=[f"IBAN: {_german_iban('37040044', '532013000')}", "BIC: COBADEFFXXX"],
)

DE_002 = DemoInvoice(
    "DEMO-02_incorrect_vat_math.pdf", "Rechnung DEMO-DE-002", "DEMO-DE-002",
    ["Weststadt IT Services GmbH", "Adalbertsteinweg 44", "52070 Aachen, Deutschland", f"USt-IdNr.: {WESTSTADT_VAT}"],
    [SELF_NAME, *SELF_ADDRESS],
    [("Rechnungsnummer", "DEMO-DE-002"), ("Rechnungsdatum", "03.11.2026"), ("Leistungsdatum", "31.10.2026"), ("Währung", "EUR")],
    [("IT consulting services", "1", "1,000.00", "1,000.00", "19%", "180.00")],
    ("1,000.00", "180.00", "1,180.00"), "EUR",
    notes=["Demo defect: 19% of 1,000.00 is 190.00, the invoice states 180.00."],
)

IN_DE_001 = DemoInvoice(
    "DEMO-03_india_to_germany_services.pdf", "EXPORT INVOICE DEMO-IN-DE-001", "DEMO-IN-DE-001",
    ["Deccan Analytics Pvt. Ltd.", "Plot 18, Hitech City", "Hyderabad, Telangana, India", f"GSTIN: {DECCAN_GSTIN}"],
    [SELF_NAME, "Theaterstraße 8", "52062 Aachen, Germany", f"VAT ID: {SELF_VAT}"],
    [("Invoice No.", "DEMO-IN-DE-001"), ("Invoice Date", "04-Nov-2026"), ("Service Period", "01-31 Oct 2026"), ("Currency", "INR")],
    [("Data engineering and analytics services", "1", "250,000.00", "250,000.00", "0%", "0.00")],
    ("250,000.00", "0.00", "250,000.00"), "INR",
    notes=["Supply meant for export under LUT without payment of integrated tax.",
           "B2B services to a German business. Tax treatment in Germany to be confirmed by the recipient's advisor."],
)

DE_IN_001 = DemoInvoice(
    "DEMO-04_germany_to_india_consulting_zugferd.pdf", "Rechnung DEMO-DE-IN-001", "DEMO-DE-IN-001",
    [SELF_NAME, *SELF_ADDRESS, f"USt-IdNr.: {SELF_VAT}"],
    ["Lotus Digital Services Pvt. Ltd.", "Andheri East", "Mumbai, Maharashtra, India", f"GSTIN: {LOTUS_GSTIN}"],
    [("Rechnungsnummer", "DEMO-DE-IN-001"), ("Rechnungsdatum", "05.11.2026"), ("Leistungsdatum", "05.11.2026"),
     ("Fällig am", "05.12.2026"), ("Währung", "EUR")],
    [("Strategy consulting services (remote)", "5", "1,000.00", "5,000.00", "0%", "0.00")],
    ("5,000.00", "0.00", "5,000.00"), "EUR",
    notes=["Nicht im Inland steuerbare Leistung (§ 3a Abs. 2 UStG).",
           "Reverse charge: the recipient is liable for tax in India. ZUGFeRD/Factur-X XML embedded."],
    payment=[f"IBAN: {_german_iban('39050000', '1074521')}", "BIC: AACSDE33XXX"],
)

IN_002 = DemoInvoice(
    "DEMO-05_indian_malformed_gstin.pdf", "TAX INVOICE DEMO-IN-002", "DEMO-IN-002",
    ["Kaveri Cloud Services Pvt. Ltd.", "No. 42, Residency Road", "Bengaluru, Karnataka, India", f"GSTIN: {MALFORMED_GSTIN}"],
    ["Blue Banyan Retail Pvt. Ltd.", "No. 7, MG Road", "Bengaluru, Karnataka, India", f"GSTIN: {BANYAN_GSTIN}"],
    [("Invoice No.", "DEMO-IN-002"), ("Invoice Date", "06-Nov-2026"), ("Place of Supply", "Karnataka (29)"), ("Currency", "INR")],
    [("Software licence", "1", "40,000.00", "40,000.00", "CGST 9% / SGST 9%", "7,200.00")],
    ("40,000.00", "7,200.00", "47,200.00"), "INR",
    notes=["Demo defect: the supplier GSTIN is malformed (14 characters, no 'Z')."],
)


def _zugferd_cii(invoice: DemoInvoice) -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100" xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100" xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocumentContext><ram:GuidelineSpecifiedDocumentContextParameter><ram:ID>urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended</ram:ID></ram:GuidelineSpecifiedDocumentContextParameter></rsm:ExchangedDocumentContext>
  <rsm:ExchangedDocument><ram:ID>DEMO-DE-IN-001</ram:ID><ram:TypeCode>380</ram:TypeCode><ram:IssueDateTime><udt:DateTimeString format="102">20261105</udt:DateTimeString></ram:IssueDateTime>
    <ram:IncludedNote><ram:Content>{escape(FICTIONAL_NOTICE)}</ram:Content></ram:IncludedNote></rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>1</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>Strategy consulting services (remote)</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement><ram:NetPriceProductTradePrice><ram:ChargeAmount>1000.00</ram:ChargeAmount></ram:NetPriceProductTradePrice></ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode="DAY">5</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement><ram:ApplicableTradeTax><ram:TypeCode>VAT</ram:TypeCode><ram:CategoryCode>O</ram:CategoryCode><ram:RateApplicablePercent>0</ram:RateApplicablePercent></ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation><ram:LineTotalAmount>5000.00</ram:LineTotalAmount></ram:SpecifiedTradeSettlementLineMonetarySummation></ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty><ram:Name>{SELF_NAME}</ram:Name><ram:PostalTradeAddress><ram:PostcodeCode>52062</ram:PostcodeCode><ram:LineOne>Theaterstraße 8</ram:LineOne><ram:CityName>Aachen</ram:CityName><ram:CountryID>DE</ram:CountryID></ram:PostalTradeAddress>
        <ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{SELF_VAT}</ram:ID></ram:SpecifiedTaxRegistration></ram:SellerTradeParty>
      <ram:BuyerTradeParty><ram:Name>Lotus Digital Services Pvt. Ltd.</ram:Name><ram:PostalTradeAddress><ram:LineOne>Andheri East</ram:LineOne><ram:CityName>Mumbai, Maharashtra</ram:CityName><ram:CountryID>IN</ram:CountryID></ram:PostalTradeAddress>
        <ram:SpecifiedTaxRegistration><ram:ID schemeID="FC">{LOTUS_GSTIN}</ram:ID></ram:SpecifiedTaxRegistration></ram:BuyerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery><ram:ActualDeliverySupplyChainEvent><ram:OccurrenceDateTime><udt:DateTimeString format="102">20261105</udt:DateTimeString></ram:OccurrenceDateTime></ram:ActualDeliverySupplyChainEvent></ram:ApplicableHeaderTradeDelivery>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:SpecifiedTradeSettlementPaymentMeans><ram:TypeCode>58</ram:TypeCode><ram:PayeePartyCreditorFinancialAccount><ram:IBANID>{_german_iban('39050000', '1074521')}</ram:IBANID></ram:PayeePartyCreditorFinancialAccount>
        <ram:PayeeSpecifiedCreditorFinancialInstitution><ram:BICID>AACSDE33XXX</ram:BICID></ram:PayeeSpecifiedCreditorFinancialInstitution></ram:SpecifiedTradeSettlementPaymentMeans>
      <ram:ApplicableTradeTax><ram:CalculatedAmount>0.00</ram:CalculatedAmount><ram:TypeCode>VAT</ram:TypeCode><ram:ExemptionReason>Nicht im Inland steuerbare Leistung (§ 3a Abs. 2 UStG)</ram:ExemptionReason><ram:BasisAmount>5000.00</ram:BasisAmount><ram:CategoryCode>O</ram:CategoryCode><ram:RateApplicablePercent>0</ram:RateApplicablePercent></ram:ApplicableTradeTax>
      <ram:SpecifiedTradePaymentTerms><ram:Description>Zahlbar innerhalb von 30 Tagen</ram:Description><ram:DueDateDateTime><udt:DateTimeString format="102">20261205</udt:DateTimeString></ram:DueDateDateTime></ram:SpecifiedTradePaymentTerms>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation><ram:LineTotalAmount>5000.00</ram:LineTotalAmount><ram:TaxBasisTotalAmount>5000.00</ram:TaxBasisTotalAmount><ram:TaxTotalAmount currencyID="EUR">0.00</ram:TaxTotalAmount><ram:GrandTotalAmount>5000.00</ram:GrandTotalAmount><ram:DuePayableAmount>5000.00</ram:DuePayableAmount></ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""
    return xml.encode("utf-8")


def _xrechnung_ubl_duplicate() -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- {FICTIONAL_NOTICE} -->
<ubl:Invoice xmlns:ubl="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0</cbc:CustomizationID>
  <cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>
  <cbc:ID>DEMO-DE-001</cbc:ID>
  <cbc:IssueDate>2026-11-02</cbc:IssueDate>
  <cbc:DueDate>2026-11-16</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:Note>Resent as XRechnung - same invoice as DEMO-01 PDF (duplicate test case).</cbc:Note>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cbc:BuyerReference>04011000-12345-34</cbc:BuyerReference>
  <cac:InvoicePeriod><cbc:StartDate>2026-10-30</cbc:StartDate><cbc:EndDate>2026-10-30</cbc:EndDate></cac:InvoicePeriod>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PostalAddress><cbc:StreetName>Musterstraße 12</cbc:StreetName><cbc:CityName>Aachen</cbc:CityName><cbc:PostalZone>52062</cbc:PostalZone><cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    <cac:PartyTaxScheme><cbc:CompanyID>{NORDLICHT_VAT}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyLegalEntity><cbc:RegistrationName>Nordlicht Bürobedarf GmbH</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PostalAddress><cbc:StreetName>Theaterstraße 8</cbc:StreetName><cbc:CityName>Aachen</cbc:CityName><cbc:PostalZone>52062</cbc:PostalZone><cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    <cac:PartyTaxScheme><cbc:CompanyID>{SELF_VAT}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyLegalEntity><cbc:RegistrationName>{SELF_NAME}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode><cac:PayeeFinancialAccount><cbc:ID>{_german_iban('37040044', '532013000')}</cbc:ID><cac:FinancialInstitutionBranch><cbc:ID>COBADEFFXXX</cbc:ID></cac:FinancialInstitutionBranch></cac:PayeeFinancialAccount></cac:PaymentMeans>
  <cac:PaymentTerms><cbc:Note>14 Tage</cbc:Note></cac:PaymentTerms>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">38.00</cbc:TaxAmount>
    <cac:TaxSubtotal><cbc:TaxableAmount currencyID="EUR">200.00</cbc:TaxableAmount><cbc:TaxAmount currencyID="EUR">38.00</cbc:TaxAmount><cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>19</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal><cbc:LineExtensionAmount currencyID="EUR">200.00</cbc:LineExtensionAmount><cbc:TaxExclusiveAmount currencyID="EUR">200.00</cbc:TaxExclusiveAmount><cbc:TaxInclusiveAmount currencyID="EUR">238.00</cbc:TaxInclusiveAmount><cbc:PayableAmount currencyID="EUR">238.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
  <cac:InvoiceLine><cbc:ID>1</cbc:ID><cbc:InvoicedQuantity unitCode="H87">4</cbc:InvoicedQuantity><cbc:LineExtensionAmount currencyID="EUR">200.00</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>Büromaterial und Druckerzubehör</cbc:Name><cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>19</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory></cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="EUR">50.00</cbc:PriceAmount></cac:Price></cac:InvoiceLine>
</ubl:Invoice>"""
    return xml.encode("utf-8")


def demo_files() -> list[tuple[str, bytes, str]]:
    """(filename, content, description) for the six demo test cases, in upload order."""
    zugferd = _draw(DE_IN_001)
    zugferd.embfile_add("factur-x.xml", _zugferd_cii(DE_IN_001), filename="factur-x.xml", desc="Factur-X/ZUGFeRD invoice data")
    zugferd_bytes = zugferd.tobytes(garbage=3, deflate=True)
    zugferd.close()
    return [
        (DE_001.filename, _pdf(DE_001), "Valid German domestic invoice with 19% VAT"),
        (DE_002.filename, _pdf(DE_002), "German invoice with incorrect VAT arithmetic"),
        (IN_DE_001.filename, _pdf(IN_DE_001), "Indian supplier to German business, B2B services"),
        (DE_IN_001.filename, zugferd_bytes, "German supplier to Indian business, consulting (ZUGFeRD hybrid PDF)"),
        (IN_002.filename, _pdf(IN_002), "Indian invoice with malformed GSTIN"),
        ("DEMO-06_duplicate_of_DEMO-01_xrechnung.xml", _xrechnung_ubl_duplicate(), "Duplicate of invoice 1, resent as XRechnung XML"),
    ]


DEMO_CHART = [
    ("4930", "Bürobedarf (demo)", "expense"),
    ("4900", "Sonstige betriebliche Aufwendungen (demo)", "expense"),
    ("8338", "Erlöse Drittland, im Inland nicht steuerbar (demo - verify)", "revenue"),
    ("8400", "Erlöse 19 % USt (demo)", "revenue"),
    ("70000", "Kreditor - demo suppliers", "creditor"),
    ("10000", "Debitor - demo customers", "debtor"),
]

DEMO_MAPPINGS = [
    {"name": "Foreign services - reverse charge (demo)", "keywords": ["data engineering", "export"], "expense_account": "4900",
     "creditor_account": "70000", "tax_key": None, "direction": "incoming", "priority": 10,
     "tax_treatment": "§13b UStG reverse charge: self-assess German VAT - set the BU key with your advisor"},
    {"name": "Office supplies (demo)", "keywords": ["Büromaterial", "office supplies", "Druckerzubehör"], "expense_account": "4930",
     "creditor_account": "70000", "tax_key": "9", "direction": "incoming", "priority": 20,
     "tax_treatment": "Input VAT 19 % (BU 9)"},
    {"name": "IT services and software (demo)", "keywords": ["IT consulting", "software", "licence", "hosting"], "expense_account": "4900",
     "creditor_account": "70000", "tax_key": "9", "direction": "incoming", "priority": 30,
     "tax_treatment": "Input VAT 19 % (BU 9)"},
    {"name": "Consulting sales to third countries (demo)", "keywords": ["consulting"], "expense_account": "8338",
     "creditor_account": "10000", "tax_key": None, "direction": "outgoing", "priority": 10,
     "tax_treatment": "Not taxable in Germany (§3a Abs. 2 UStG); no German VAT"},
]


def write_demo_folder(target) -> list[str]:
    from pathlib import Path

    folder = Path(target)
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for filename, content, _ in demo_files():
        (folder / filename).write_bytes(content)
        names.append(filename)
    return names

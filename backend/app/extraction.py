import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymupdf
from lxml import etree

from app.einvoice import EMBEDDED_XML_NAMES, extract_structured, parse_xml_bytes
from app.llm import extract_with_llm
from app.ocr import available_provider, run_ocr

# Minimum number of non-whitespace characters before a PDF counts as machine-readable.
MIN_TEXT_CHARACTERS = 20


@dataclass
class Extraction:
    fields: dict[str, object]
    text: str
    method: str  # "text", "ocr", "ocr_unavailable", "xml", "zugferd"
    warnings: list[str] = field(default_factory=list)
    field_meta: dict[str, dict[str, object]] = field(default_factory=dict)
    pages: list[dict[str, float]] = field(default_factory=list)
    provider: str = "rules"
    embedded_xml: bytes | None = None


@dataclass(frozen=True)
class Segment:
    """A run of words on one visual line, separated from neighbours by a column gap."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    confidence: float = 1.0

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2


# Base confidence per extraction source; OCR results are additionally scaled by OCR scores.
SOURCE_CONFIDENCE = {"xml": 0.99, "label": 0.92, "layout": 0.85, "table": 0.85, "pattern": 0.8, "heuristic": 0.7, "llm": 0.6}
HEURISTIC_FIELDS = {"supplier_country", "customer_country", "transaction_direction", "advisor_review_required", "tax_type", "description", "document_type", "exchange_rate_status"}


def extract_invoice(path: Path, content_type: str) -> tuple[dict[str, object], str]:
    result = extract_document(path, content_type)
    return result.fields, result.text


def extract_document(path: Path, content_type: str) -> Extraction:
    if content_type != "application/pdf":
        return _extract_xml_file(path)

    warnings: list[str] = []
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError("Password-protected PDFs are not supported")
        pages = [{"width": page.rect.width, "height": page.rect.height} for page in document]
        embedded = _embedded_einvoice(document)
        text = "\n".join(page.get_text() for page in document)
        words = [(*w, 1.0) for w in _words(document)]
        method = "text"
        if len(re.sub(r"\s", "", text)) < MIN_TEXT_CHARACTERS:
            result = run_ocr(document)
            if result is None:
                method = "ocr_unavailable"
                warnings.append(
                    "This PDF has no machine-readable text and no OCR engine is available on the server "
                    "(install PaddleOCR or Tesseract). Enter the invoice details manually."
                )
            else:
                text, words = result.text, result.words
                method = "ocr"
                warnings.append(f"Fields were read with OCR ({result.provider}). Verify every value against the original document.")

    segments = _segments(words)
    fields, meta = _rule_fields(text, segments)
    provider = "rules"

    if embedded is not None:
        xml_fields, fmt = embedded[0]
        for key, value in xml_fields.items():
            fields[key] = value
            meta[key] = {"source": "xml", "confidence": SOURCE_CONFIDENCE["xml"]}
        method = "zugferd"
        warnings.append(f"Structured invoice data ({fmt}) embedded in the PDF was used; it takes precedence over the printed text.")

    llm_fields, llm_warning = extract_with_llm(text) if method != "ocr_unavailable" else ({}, None)
    if llm_warning:
        warnings.append(llm_warning)
    for key, value in llm_fields.items():
        if key not in fields:
            fields[key] = value
            meta[key] = {"source": "llm", "confidence": SOURCE_CONFIDENCE["llm"]}
            provider = "rules+llm"

    _attach_locations(fields, meta, segments, ocr=method == "ocr")
    return Extraction(fields, text, method, warnings, meta, pages, provider, embedded[1] if embedded else None)


def _rule_fields(text: str, segments: list[Segment]) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    meta: dict[str, dict[str, object]] = {}
    fields = extract_pdf_fields(text)
    for key in fields:
        source = "heuristic" if key in HEURISTIC_FIELDS else "label"
        meta[key] = {"source": source, "confidence": SOURCE_CONFIDENCE[source]}

    def put(key: str, value: object, source: str, override: bool = False) -> None:
        if value in (None, "", []) or (key in fields and not override):
            return
        fields[key] = value
        meta[key] = {"source": source, "confidence": SOURCE_CONFIDENCE[source]}

    parties = _parties_from_layout(segments)
    if parties:
        for prefix in ("supplier", "customer"):
            for suffix in ("name", "address", "tax_id", "tax_number"):
                if fields.pop(f"{prefix}_{suffix}", None) is not None:
                    meta.pop(f"{prefix}_{suffix}", None)
    for key, value in parties.items():
        put(key, value, "layout", override=True)
    if parties:
        _derive_party_ids(fields)
    for key in ("supplier_vat_id", "customer_vat_id", "supplier_gstin", "customer_gstin", "supplier_tax_number", "customer_tax_number"):
        if key in fields and key not in meta:
            meta[key] = {"source": "layout", "confidence": SOURCE_CONFIDENCE["layout"]}
    _derive_countries(fields, text)

    before = set(fields)
    _fill_from_layout(fields, segments)
    for key in set(fields) - before:
        meta[key] = {"source": "layout", "confidence": SOURCE_CONFIDENCE["layout"]}
    before = set(fields)
    _fill_currency(fields, text)
    for key in set(fields) - before:
        meta[key] = {"source": "pattern", "confidence": SOURCE_CONFIDENCE["pattern"]}

    for key, value in _payment_and_dates(text).items():
        put(key, value, "label" if key in ("due_date", "payment_terms") else "pattern")
    put("tax_exemption_reference", _exemption_reference(text), "pattern")
    items = _line_items_from_layout(segments)
    if items:
        fields["line_items"] = items
        meta["line_items"] = {"source": "table", "confidence": SOURCE_CONFIDENCE["table"]}
        rates = {str(item.get("vat_rate")) for item in items if item.get("vat_rate") is not None}
        if "vat_rate" not in fields and len(rates) == 1:
            put("vat_rate", rates.pop(), "table")
            fields.setdefault("tax_type", "VAT")
    return fields, meta


def _extract_xml_file(path: Path) -> Extraction:
    root = parse_xml_bytes(path.read_bytes())
    structured = extract_structured(root)
    text = etree.tostring(root, encoding="unicode")
    if structured:
        fields, fmt = structured
        meta = {key: {"source": "xml", "confidence": SOURCE_CONFIDENCE["xml"]} for key in fields}
        _derive_countries(fields)
        return Extraction(fields, text, "xml", [f"Native {fmt} e-invoice data was used."], meta, provider="xml")
    fields = _xml_values(root)
    meta = {key: {"source": "xml", "confidence": 0.8} for key in fields}
    return Extraction(fields, text, "xml", ["Generic XML: only common invoice elements were recognised."], meta, provider="xml")


def _embedded_einvoice(document: pymupdf.Document) -> tuple[tuple[dict[str, object], str], bytes] | None:
    try:
        names = document.embfile_names()
    except Exception:
        return None
    candidates = sorted(names, key=lambda name: (name.lower() not in [n.lower() for n in EMBEDDED_XML_NAMES], name))
    for name in candidates:
        if not name.lower().endswith(".xml"):
            continue
        try:
            content = document.embfile_get(name)
            structured = extract_structured(parse_xml_bytes(content))
        except Exception:
            continue
        if structured:
            return structured, content
    return None


def ocr_available() -> bool:
    return available_provider() is not None


def _words(document: pymupdf.Document) -> list[tuple]:
    return [(index, *word[:5]) for index, page in enumerate(document) for word in page.get_text("words")]


def _segments(words: list[tuple], row_tolerance: float = 4.0, column_gap: float = 15.0) -> list[Segment]:
    segments: list[Segment] = []
    ordered = sorted(words, key=lambda word: (word[0], round((word[2] + word[4]) / 2), word[1]))
    rows: list[list[tuple]] = []
    for word in ordered:
        center = (word[2] + word[4]) / 2
        row = rows[-1] if rows else None
        if row and row[0][0] == word[0] and abs((row[0][2] + row[0][4]) / 2 - center) <= row_tolerance:
            row.append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda word: word[1])
        current = [row[0]]
        for word in row[1:]:
            if word[1] - current[-1][3] > column_gap:
                segments.append(_segment(current))
                current = [word]
            else:
                current.append(word)
        segments.append(_segment(current))
    return segments


def _segment(words: list[tuple]) -> Segment:
    return Segment(
        page=words[0][0],
        x0=min(word[1] for word in words),
        y0=min(word[2] for word in words),
        x1=max(word[3] for word in words),
        y1=max(word[4] for word in words),
        text=" ".join(str(word[5]) for word in words),
        confidence=min(float(word[6]) if len(word) > 6 else 1.0 for word in words),
    )


# Ordered by priority: earlier patterns for the same field win.
LAYOUT_LABELS: list[tuple[str, str]] = [
    ("invoice_number", r"(?:invoice\s*(?:#|no\.?|number|nr\.?)|rechnungs-?(?:nr\.?|nummer)|#)"),
    ("invoice_date", r"(?:invoice\s*date|rechnungsdatum|date|datum)"),
    ("net_amount", r"(?:sub\s*-?\s*total|net\s*(?:total|amount)?|nettobetrag|zwischensumme)"),
    ("vat_amount", r"(?:(?:vat|sales\s*tax|tax|mwst\.?|ust\.?|gst)(?:\s*(?:amount|total))?(?:\s*\([^)]*\))?)"),
    ("gross_amount", r"(?:grand\s*total|total\s*gross|gross\s*total|total|gesamtbetrag|rechnungsbetrag|brutto)"),
    ("gross_amount", r"(?:total\s*due|amount\s*due|balance\s*due)"),
    ("customer_name", r"(?:bill(?:ed)?\s*to|sold\s*to|customer|rechnungsempfänger|kunde)"),
]


def _fill_from_layout(fields: dict[str, object], segments: list[Segment]) -> None:
    """Pair labels with values to their right or directly below; only fills missing fields."""
    for target, label in LAYOUT_LABELS:
        if target in fields:
            continue
        pattern = re.compile(rf"^{label}\s*[:\-]?\s*(.*)$", re.IGNORECASE)
        for segment in segments:
            match = pattern.match(segment.text.strip())
            if not match:
                continue
            candidates = [match.group(1).strip()] if match.group(1).strip() else []
            candidates += [neighbour.text for neighbour in _neighbours(segment, segments)]
            value = next((v for v in (_layout_value(target, c) for c in candidates) if v is not None), None)
            if value is not None:
                fields[target] = value
                break


def _neighbours(label: Segment, segments: list[Segment]) -> list[Segment]:
    same_page = [s for s in segments if s.page == label.page and s is not label]
    right = sorted(
        (s for s in same_page if abs(s.y_center - label.y_center) <= 3 and s.x0 >= label.x1),
        key=lambda s: s.x0,
    )
    below = sorted(
        (
            s
            for s in same_page
            if 0 <= s.y0 - label.y1 + 2 <= 22 and s.x0 < label.x1 + 30 and s.x1 > label.x0 - 5
        ),
        key=lambda s: s.y0,
    )
    return right[:1] + below[:1]


def _layout_value(target: str, value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if target.endswith("_amount"):
        if not re.fullmatch(r"[-–]?\s*[^\d\s-]{0,3}\s*[-–]?\s*\d[\d.,\s]*\s*[^\d\s]{0,3}", value):
            return None
        amount = _normalise_amount(value.replace("–", "-"))
        return amount if re.fullmatch(r"-?\d+(?:\.\d+)?", amount) else None
    if target == "invoice_date":
        date = _normalise_date(value)
        return date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None
    if target == "invoice_number":
        return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._-]{0,39}", value) and re.search(r"\d", value) else None
    if target == "customer_name":
        return value if re.search(r"[A-Za-zÀ-ÿ]{2}", value) and not _looks_like_amount(value) else None
    return value


def _looks_like_amount(value: str) -> bool:
    return bool(re.fullmatch(r"[^\w]*[\d.,\s-]+[^\w]*", value))


def _fill_currency(fields: dict[str, object], text: str) -> None:
    if fields.get("currency"):
        return
    symbols = {"EUR": "€" in text, "INR": "₹" in text, "GBP": "£" in text}
    if "$" in text and not re.search(r"\b(?:CAD|AUD|NZD|SGD|HKD)\b|[A-Z]{1,2}\$", text):
        symbols["USD"] = True
    found = [code for code, present in symbols.items() if present]
    if not found:
        # Fall back to ISO codes written next to amounts, e.g. "EUR 200.00".
        found = sorted(set(re.findall(r"\b(EUR|INR|USD|GBP|CHF)\b", text)))
    if len(found) == 1:
        fields["currency"] = found[0]


def extract_pdf_fields(text: str) -> dict[str, object]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fields: dict[str, object] = {}
    labels = {
        "invoice_number": r"^(?:rechnungsnummer|gutschriftnummer|invoice\s*(?:no\.?|number)|credit\s*note\s*(?:no\.?|number))$",
        "invoice_date": r"^(?:rechnungsdatum|invoice\s*date|datum)$",
        "service_date": r"^(?:leistungsdatum|lieferdatum|leistungszeitraum|service\s*(?:date|period)|delivery\s*date)$",
        "reference_invoice_number": r"^(?:bezug|referenz|original\s*invoice(?:\s*(?:no\.?|number))?|reference)$",
        "currency": r"^(?:währung|currency)$",
        "place_of_supply": r"^(?:place of supply|leistungsort)$",
        "net_amount": r"^(?:net total|nettobetrag|netto gesamt)$",
        "vat_amount": r"^(?:vat total|mwst total|ust total|gst total|tax total)$",
        "gross_amount": r"^(?:gross total|total gross|gesamtbetrag|brutto gesamt)$",
    }
    for field, label_pattern in labels.items():
        for index, line in enumerate(lines):
            inline = re.match(label_pattern + r"\s*[:\-]\s*(.+)$", line, re.IGNORECASE)
            if inline:
                value = inline.group(1).strip()
            elif re.fullmatch(label_pattern, line, re.IGNORECASE) and index + 1 < len(lines):
                value = lines[index + 1]
            else:
                continue
            if field in {"net_amount", "vat_amount", "gross_amount"}:
                value = _normalise_amount(value)
            elif field in {"invoice_date", "service_date"}:
                value = _normalise_date(value)
            elif field == "currency":
                value = value.upper()
            fields[field] = value
            break

    inline_patterns = {
        "supplier_name": r"^(?:supplier|seller|from|lieferant|rechnungssteller)\s*[:\-]\s*(.+)$",
        "supplier_address": r"^(?:supplier address|seller address)\s*[:\-]\s*(.+)$",
        "customer_name": r"^(?:customer|buyer|bill to|kunde)\s*[:\-]\s*(.+)$",
        "customer_address": r"^(?:customer address|buyer address|bill to address)\s*[:\-]\s*(.+)$",
        "invoice_number": r"^(?:invoice\s*(?:no\.?|number)|rechnungsnummer)\s*[:#-]\s*(.+)$",
        "invoice_date": r"^(?:invoice date|rechnungsdatum)\s*[:\-]\s*(.+)$",
        "net_amount": r"^(?:net(?: total)?|subtotal|nettobetrag)\s*[:\-]\s*(.+)$",
        "vat_amount": r"^(?:vat amount|vat total|mwst|ust)\s*[:\-]\s*(.+)$",
        "gross_amount": r"^(?:gross total|total|gesamtbetrag|brutto)\s*[:\-]\s*(.+)$",
        "description": r"^(?:description|service|goods|leistung|beschreibung)\s*[:\-]\s*(.+)$",
    }
    for field, pattern in inline_patterns.items():
        if field in fields:
            continue
        match = next((re.match(pattern, line, re.IGNORECASE) for line in lines if re.match(pattern, line, re.IGNORECASE)), None)
        if match:
            value = match.group(1).strip()
            fields[field] = _normalise_amount(value) if field.endswith("_amount") else _normalise_date(value) if field == "invoice_date" else value

    rate_inline = re.search(r"(?im)^\s*(?:vat|mwst|ust)\s*[:\-]\s*(\d{1,2}(?:[.,]\d+)?)\s*%", text)
    if rate_inline and "vat_rate" not in fields:
        fields["vat_rate"] = str(Decimal(rate_inline.group(1).replace(",", ".")))

    party_start = next((index + 1 for index, line in enumerate(lines) if line.upper() == "CUSTOMER"), None)
    if party_start is not None and party_start < len(lines):
        fields["supplier_name"] = lines[party_start]
        customer_name_index = None
        supplier_address: list[str] = []
        for index in range(party_start + 1, len(lines)):
            line = lines[index]
            tax_id = re.match(r"^(?:GSTIN|VAT\s*ID|USt-IdNr\.?|VAT\s*number)\s*:\s*(\S+)", line, re.IGNORECASE)
            if tax_id:
                fields["supplier_tax_id"] = tax_id.group(1)
                continue
            if _looks_like_address(line) or not supplier_address:
                supplier_address.append(line)
                continue
            if _is_metadata_line(line):
                break
            customer_name_index = index
            break
        if supplier_address:
            fields["supplier_address"] = ", ".join(supplier_address)
        if customer_name_index is not None:
            fields["customer_name"] = lines[customer_name_index]
            customer_address: list[str] = []
            for line in lines[customer_name_index + 1 :]:
                tax_id = re.match(r"^(?:GSTIN|VAT\s*ID|USt-IdNr\.?|VAT\s*number)\s*:\s*(\S+)", line, re.IGNORECASE)
                if tax_id:
                    fields["customer_tax_id"] = tax_id.group(1)
                    continue
                if _looks_like_address(line) or not customer_address:
                    customer_address.append(line)
                    continue
                break
            if customer_address:
                fields["customer_address"] = ", ".join(customer_address)

    for field, tax_field in (("supplier_gstin", "supplier_tax_id"), ("customer_gstin", "customer_tax_id")):
        tax_id = str(fields.get(tax_field, ""))
        if re.fullmatch(r"[0-9A-Z]{15}", tax_id, re.IGNORECASE):
            fields[field] = tax_id

    if str(fields.get("supplier_tax_id", "")).upper().startswith("DE"):
        fields["supplier_vat_id"] = fields["supplier_tax_id"]
    if str(fields.get("customer_tax_id", "")).upper().startswith("DE"):
        fields["customer_vat_id"] = fields["customer_tax_id"]

    # Only percentages in a tax context count as tax rates, so discounts, interest or
    # skonto percentages are never mistaken for the VAT/GST rate.
    tax_context = re.compile(r"\b(?:vat|mwst|ust|gst|cgst|sgst|igst|tax|steuer)\b", re.IGNORECASE)
    rate_matches = [
        rate
        for line in lines
        if tax_context.search(line) or re.fullmatch(r"-?\d{1,2}(?:[.,]\d+)?\s*%", line)
        for rate in re.findall(r"(?<!\w)(\d{1,2}(?:[.,]\d+)?)\s*%", line)
    ]
    if rate_matches:
        rates = [Decimal(rate.replace(",", ".")) for rate in rate_matches]
        if re.search(r"\bCGST\b", text, re.IGNORECASE) and re.search(r"\bSGST\b", text, re.IGNORECASE):
            fields["tax_type"] = "CGST + SGST"
            fields["cgst_rate"] = str(rates[0])
            fields["sgst_rate"] = str(rates[1] if len(rates) > 1 else rates[0])
            fields["vat_rate"] = str(sum(rates[:2], Decimal("0")))
        elif re.search(r"\bIGST\b", text, re.IGNORECASE):
            fields["tax_type"] = "IGST"
            fields["igst_rate"] = str(rates[0])
            fields["vat_rate"] = str(rates[0])
        elif re.search(r"\b(?:No VAT|0%\s+shown)\b", text, re.IGNORECASE):
            fields["tax_type"] = "reverse_charge_or_zero_rated_review"
            fields["vat_rate"] = "0"
        else:
            fields["tax_type"] = "VAT"
            fields["vat_rate"] = str(rates[0])
    elif re.search(r"\bNo VAT\b|(?<!\d)0%\s+shown\b", text, re.IGNORECASE):
        fields["tax_type"] = "reverse_charge_or_zero_rated_review"
        fields["vat_rate"] = "0"

    description_index = next((i for i, line in enumerate(lines) if line.casefold() == "vat amount"), None)
    if description_index is not None and description_index + 1 < len(lines):
        fields["description"] = lines[description_index + 1]

    fields["document_type"] = "credit_note" if re.search(r"\b(?:gutschrift|credit note)\b", text, re.IGNORECASE) else "invoice"
    reverse_text = re.search(r"reverse[- ]charge|steuerschuldnerschaft des leistungsempfängers", text, re.IGNORECASE)
    if reverse_text:
        fields["reverse_charge_wording"] = reverse_text.group(0)
    supplier_country = "India" if re.search(r"India", str(fields.get("supplier_address", "")), re.IGNORECASE) else "Germany" if re.search(r"Deutschland|Germany", str(fields.get("supplier_address", "")), re.IGNORECASE) else None
    customer_country = "India" if re.search(r"India", str(fields.get("customer_address", "")), re.IGNORECASE) else "Germany" if re.search(r"Deutschland|Germany", str(fields.get("customer_address", "")), re.IGNORECASE) else None
    if supplier_country:
        fields["supplier_country"] = supplier_country
    if customer_country:
        fields["customer_country"] = customer_country
    if supplier_country and customer_country and supplier_country != customer_country:
        fields["transaction_direction"] = f"{supplier_country}_to_{customer_country}"
        fields["advisor_review_required"] = "true"
        exchange_rate = re.search(r"exchange rate\s*[:\-]\s*([0-9.,]+)", text, re.IGNORECASE)
        if exchange_rate:
            fields["exchange_rate"] = _normalise_amount(exchange_rate.group(1))
            fields["exchange_rate_status"] = "provided"
        elif re.search(r"no rate supplied|exchange.rate note", text, re.IGNORECASE):
            fields["exchange_rate_status"] = "missing"
    return fields


def _looks_like_address(value: str) -> bool:
    return bool(
        re.search(r"\d|straße|strasse|street|road|avenue|plot|hitech|musterstraße|ad[a-z]+", value, re.IGNORECASE)
        or re.search(r",\s*(?:Deutschland|Germany|India|Telangana|Maharashtra)", value, re.IGNORECASE)
    )


def _is_metadata_line(value: str) -> bool:
    return bool(
        re.match(
            r"^(?:rechnungs|gutschrift|invoice|credit note|datum|date|währung|currency|place of supply|description|qty|unit price|net|vat|notes|gstin|vat id|ust-idnr)",
            value,
            re.IGNORECASE,
        )
    )


def _xml_values(root: etree._Element) -> dict[str, object]:
    names = {
        "invoice_number": {"id", "invoicenumber", "invoiceno"},
        "invoice_date": {"issuedate", "invoicedate"},
        "supplier_name": {"registrationname", "sellername", "suppliername"},
        "customer_name": {"buyername", "customername"},
        "currency": {"documentcurrencycode", "currencycode"},
        "net_amount": {"taxexclusiveamount", "lineextensionamount"},
        "vat_amount": {"taxamount"},
        "gross_amount": {"taxinclusiveamount", "payableamount"},
        "vat_rate": {"percent", "rate"},
    }
    results: dict[str, object] = {}
    for element in root.iter():
        local_name = etree.QName(element).localname.lower()
        value = (element.text or "").strip()
        if not value:
            continue
        for field, aliases in names.items():
            if field not in results and local_name in aliases:
                results[field] = _normalise_amount(value) if field.endswith("_amount") else value.upper() if field == "currency" else value
    return results


def _normalise_amount(value: str) -> str:
    raw = re.sub(r"[^\d,.-]", "", value)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".") if len(raw.rsplit(",", 1)[-1]) <= 2 else raw.replace(",", "")
    try:
        return str(Decimal(raw))
    except InvalidOperation:
        return value


def _normalise_date(value: str) -> str:
    value = value.strip()
    for date_format in (
        "%d-%b-%Y", "%d-%B-%Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y",
        "%d %b %Y", "%d %B %Y", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", value)
    if not match:
        return value
    day, month, year = match.groups()
    if len(year) == 2:
        year = f"20{year}"
    return f"{year}-{int(month):02d}-{int(day):02d}"


# --- Layout-aware parties -------------------------------------------------------------

SUPPLIER_LABEL = re.compile(r"^(?:supplier|seller|vendor|from|lieferant|rechnungssteller|leistungserbringer|verkäufer)\s*:?$", re.IGNORECASE)
CUSTOMER_LABEL = re.compile(r"^(?:customer|buyer|bill(?:ed)?\s*to|kunde|rechnungsempfänger|leistungsempfänger|käufer)\s*:?$", re.IGNORECASE)
TAX_ID_LINE = re.compile(
    r"^(?P<label>GSTIN|VAT\s*(?:ID|No\.?|number|reg(?:istration)?\.?\s*no\.?)|USt-?IdNr\.?|USt-?ID|Steuer-?(?:nummer|nr\.?)|St\.?-?Nr\.?|Tax\s*(?:ID|number))\s*[:.]?\s*(?P<value>\S.*)$",
    re.IGNORECASE,
)
GSTIN_SHAPE = re.compile(r"^\d{2}[0-9A-Z]{13}$")
EU_VAT_SHAPE = re.compile(r"^(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI)[0-9A-Z]{8,12}$")


def _parties_from_layout(segments: list[Segment]) -> dict[str, object]:
    """Read supplier/customer blocks column by column, independent of text order (fixes OCR interleaving)."""
    supplier = next((s for s in segments if SUPPLIER_LABEL.match(s.text.strip())), None)
    customer = next((s for s in segments if CUSTOMER_LABEL.match(s.text.strip())), None)
    if not supplier or not customer or supplier.page != customer.page or abs(supplier.y_center - customer.y_center) > 6:
        return {}
    left, right = sorted((supplier, customer), key=lambda s: s.x0)
    fields: dict[str, object] = {}
    for label, prefix in ((supplier, "supplier"), (customer, "customer")):
        x_max = right.x0 - 8 if label is left else float("inf")
        x_min = label.x0 - 8
        column = sorted(
            (s for s in segments if s.page == label.page and s.y0 >= label.y1 - 1 and x_min <= s.x0 < x_max),
            key=lambda s: s.y0,
        )
        lines: list[Segment] = []
        previous_bottom = label.y1
        for segment in column:
            if segment.y0 - previous_bottom > (20 if not lines else 14):
                break
            if _is_metadata_line(segment.text) and not TAX_ID_LINE.match(segment.text):
                break
            lines.append(segment)
            previous_bottom = segment.y1
        if not lines:
            continue
        fields[f"{prefix}_name"] = lines[0].text.strip()
        address: list[str] = []
        for line in lines[1:]:
            match = TAX_ID_LINE.match(line.text.strip())
            if match:
                label_text = match.group("label").lower()
                value = match.group("value").strip().split()[0]
                if label_text.startswith("steuer") or label_text.startswith("st"):
                    fields[f"{prefix}_tax_number"] = value
                    fields.setdefault(f"{prefix}_tax_id", value)
                else:
                    fields[f"{prefix}_tax_id"] = value
                    if label_text.startswith("gstin"):
                        # Labelled as GSTIN: keep it as one even if malformed, so validation can flag it.
                        fields[f"_{prefix}_gstin_labelled"] = value
            else:
                address.append(line.text.strip())
        if address:
            fields[f"{prefix}_address"] = ", ".join(address)
    return fields


def _derive_party_ids(fields: dict[str, object]) -> None:
    for prefix in ("supplier", "customer"):
        for key in (f"{prefix}_gstin", f"{prefix}_vat_id"):
            fields.pop(key, None)
        tax_id = str(fields.get(f"{prefix}_tax_id") or "").upper().replace(" ", "")
        labelled = fields.pop(f"_{prefix}_gstin_labelled", None)
        if labelled or GSTIN_SHAPE.fullmatch(tax_id) or re.fullmatch(r"[0-9A-Z]{15}", tax_id):
            fields[f"{prefix}_gstin"] = tax_id
        elif EU_VAT_SHAPE.fullmatch(tax_id):
            fields[f"{prefix}_vat_id"] = tax_id


COUNTRY_PATTERNS = {
    "India": r"\bIndia\b|\bIndien\b",
    "Germany": r"\bDeutschland\b|\bGermany\b",
    "United States": r"\bUSA\b|\bUnited States\b",
    "United Kingdom": r"\bUnited Kingdom\b|\bUK\b",
}


def _derive_countries(fields: dict[str, object], text: str = "") -> None:
    for prefix in ("supplier", "customer"):
        if fields.get(f"{prefix}_country"):
            continue
        address = str(fields.get(f"{prefix}_address") or "")
        country = next((name for name, pattern in COUNTRY_PATTERNS.items() if re.search(pattern, address, re.IGNORECASE)), None)
        if not country:
            if fields.get(f"{prefix}_gstin"):
                country = "India"
            elif str(fields.get(f"{prefix}_vat_id") or "").startswith("DE"):
                country = "Germany"
        if country:
            fields[f"{prefix}_country"] = country
    supplier, customer = fields.get("supplier_country"), fields.get("customer_country")
    if supplier and customer and supplier != customer:
        fields["transaction_direction"] = f"{supplier}_to_{customer}".replace(" ", "_")
        fields["advisor_review_required"] = "true"
        if "exchange_rate_status" not in fields and text and re.search(r"no rate supplied|exchange.rate note", text, re.IGNORECASE):
            fields["exchange_rate_status"] = "missing"
    elif supplier and customer:
        fields.pop("transaction_direction", None)
        fields.pop("advisor_review_required", None)


# --- Payment, dates and exemption references --------------------------------------------

IBAN_PATTERN = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?)\b")


def iban_is_valid(iban: str) -> bool:
    compact = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", compact):
        return False
    digits = "".join(str(int(ch, 36)) for ch in compact[4:] + compact[:4])
    return int(digits) % 97 == 1


def _labelled_value(lines: list[str], label: str) -> str | None:
    pattern = re.compile(rf"^(?:{label})\s*[:\-]?\s*(.*)$", re.IGNORECASE)
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            value = match.group(1).strip() or (lines[index + 1].strip() if index + 1 < len(lines) else "")
            return value or None
    return None


def _payment_and_dates(text: str) -> dict[str, object]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found: dict[str, object] = {}
    due = _labelled_value(lines, r"due\s*date|payment\s*due|fällig(?:keit|keitsdatum|\s*am)?|zahlbar\s*bis")
    if due:
        normalised = _normalise_date(due)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalised):
            found["due_date"] = normalised
    terms = _labelled_value(lines, r"zahlungsziel|payment\s*terms|zahlungsbedingungen|terms")
    if terms and len(terms) <= 120:
        found["payment_terms"] = terms
    for match in IBAN_PATTERN.finditer(text):
        if iban_is_valid(match.group(1)):
            found["iban"] = match.group(1).replace(" ", "")
            break
    bic = re.search(r"\b(?:BIC|SWIFT)(?:/SWIFT)?\s*[:\-]?\s*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", text)
    if bic:
        found["bic"] = bic.group(1)
    return found


EXEMPTION_PATTERN = re.compile(
    r"(steuerfreie?\s+(?:Ausfuhrlieferung|innergemeinschaftliche\s+Lieferung)[^.\n]*"
    r"|§\s*4\s*Nr\.?\s*\d+\s*[a-z]?\)?\s*UStG[^.\n]*"
    r"|§\s*19\s*(?:Abs\.?\s*1\s*)?UStG[^.\n]*"
    r"|Art(?:ikel|\.)\s*1(?:38|46)\s*(?:MwStSystRL|(?:EU\s*)?VAT\s*Directive|Directive\s*2006/112/EC)[^.\n]*"
    r"|(?:supply|export)[^.\n]{0,40}under\s+(?:bond\s+or\s+)?(?:LUT|letter\s+of\s+undertaking)[^.\n]*"
    r"|without\s+payment\s+of\s+(?:integrated\s+tax|IGST)[^.\n]*"
    r"|zero[- ]rated\s+(?:supply|export)[^.\n]*)",
    re.IGNORECASE,
)


def _exemption_reference(text: str) -> str | None:
    match = EXEMPTION_PATTERN.search(text)
    return re.sub(r"\s+", " ", match.group(1)).strip()[:200] if match else None


# --- Line items ----------------------------------------------------------------------------

TABLE_COLUMNS: list[tuple[str, str]] = [
    ("vat_amount", r"(?:vat|tax|gst|mwst|ust)[\s.-]*(?:amount|betrag)|steuerbetrag"),
    ("unit_price", r"unit\s*price|rate|einzelpreis|preis|price"),
    ("quantity", r"qty|quantity|menge|anzahl"),
    ("description", r"description|beschreibung|item|leistung|bezeichnung|artikel"),
    ("vat_rate", r"vat|tax|gst|mwst|ust|steuersatz|vat\s*%"),
    ("net_amount", r"net|amount|betrag|netto|gesamt|total"),
]
TOTALS_ROW = re.compile(r"^(?:net\s*total|sub\s*-?total|total|gross|zwischensumme|nettobetrag|gesamt|summe|vat\s*total|notes)", re.IGNORECASE)


def _line_items_from_layout(segments: list[Segment]) -> list[dict[str, object]]:
    header_desc = next((s for s in segments if re.fullmatch(r"(?:description|beschreibung|item|leistung|bezeichnung|artikel)", s.text.strip(), re.IGNORECASE)), None)
    if not header_desc:
        return []
    header = sorted((s for s in segments if s.page == header_desc.page and abs(s.y_center - header_desc.y_center) <= 4), key=lambda s: s.x0)
    columns: list[tuple[float, str]] = []
    for cell in header:
        name = next((key for key, pattern in TABLE_COLUMNS if re.fullmatch(pattern, cell.text.strip(), re.IGNORECASE)), None)
        if name and name not in [c[1] for c in columns]:
            columns.append((cell.x0, name))
    if len(columns) < 2 or "description" not in [c[1] for c in columns]:
        return []
    columns.sort()
    body = sorted(
        (s for s in segments if s.page == header_desc.page and s.y0 > header_desc.y1 - 1 and s.x0 >= columns[0][0] - 15),
        key=lambda s: (round(s.y_center), s.x0),
    )
    rows: list[list[Segment]] = []
    for segment in body:
        if TOTALS_ROW.match(segment.text.strip()):
            break
        if rows and abs(rows[-1][0].y_center - segment.y_center) <= 4:
            rows[-1].append(segment)
        else:
            if rows and segment.y0 - max(s.y1 for s in rows[-1]) > 30:
                break
            rows.append([segment])
    items: list[dict[str, str]] = []
    for row in rows:
        cells: dict[str, str] = {}
        for segment in row:
            name = max((c for c in columns if c[0] <= segment.x0 + 6), default=columns[0])[1]
            cells[name] = (cells.get(name, "") + " " + segment.text.strip()).strip()
        if items and ("quantity" not in cells and "net_amount" not in cells):
            for key, value in cells.items():  # continuation line of the previous item
                items[-1][key] = (items[-1].get(key, "") + " " + value).strip()
        else:
            items.append(cells)
    result = []
    for cells in items:
        item: dict[str, object] = {"description": cells.get("description")}
        quantity = cells.get("quantity")
        if quantity and re.fullmatch(r"-?\d+(?:[.,]\d+)?", quantity):
            item["quantity"] = quantity.replace(",", ".")
        for key in ("unit_price", "net_amount", "vat_amount"):
            if cells.get(key):
                amount = _normalise_amount(cells[key].replace("–", "-"))
                if re.fullmatch(r"-?\d+(?:\.\d+)?", amount):
                    item[key] = amount
        rates = re.findall(r"(\d{1,2}(?:[.,]\d+)?)\s*%", cells.get("vat_rate", ""))
        if rates:
            item["vat_rate"] = str(sum(Decimal(r.replace(",", ".")) for r in rates)) if re.search(r"CGST", cells.get("vat_rate", ""), re.I) else rates[0].replace(",", ".")
        if item.get("description") or item.get("net_amount"):
            result.append(item)
    return result


# --- Field locations (for highlighting in the review UI) --------------------------------

def _attach_locations(fields: dict[str, object], meta: dict[str, dict[str, object]], segments: list[Segment], ocr: bool) -> None:
    normalised = []
    for segment in segments:
        text = segment.text.strip()
        tail = text.split(":", 1)[1].strip() if ":" in text else text
        forms = {text.casefold(), tail.casefold()}
        for candidate in (text, tail):
            amount = _normalise_amount(candidate.replace("–", "-"))
            if re.fullmatch(r"-?\d+(?:\.\d+)?", amount):
                forms.add(amount)
            forms.add(_normalise_date(candidate).casefold())
        normalised.append((segment, forms))
    for key, value in fields.items():
        if not isinstance(value, str) or not value.strip() or key in HEURISTIC_FIELDS:
            continue
        target = value.strip().casefold()
        match = next((seg for seg, forms in normalised if target in forms), None)
        if match is None and len(target) >= 6:
            match = next((seg for seg, _ in normalised if target in seg.text.casefold() or seg.text.casefold() in target and len(seg.text) >= 6), None)
        entry = meta.setdefault(key, {"source": "heuristic", "confidence": SOURCE_CONFIDENCE["heuristic"]})
        if match is not None:
            entry["page"] = match.page
            entry["bbox"] = [round(match.x0, 1), round(match.y0, 1), round(match.x1, 1), round(match.y1, 1)]
            if ocr:
                entry["confidence"] = round(float(entry["confidence"]) * 0.9 * match.confidence, 3)
        elif ocr:
            entry["confidence"] = round(float(entry["confidence"]) * 0.8, 3)

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.tax_ids import gstin_check_character, gstin_details, vat_id_details

SIMPLIFIED_INVOICE_LIMIT = Decimal("250")
# Fields a small-amount invoice (Kleinbetragsrechnung, §33 UStDV) may omit.
SIMPLIFIED_EXEMPT_FIELDS = {"customer_name", "customer_address", "supplier_tax_id", "invoice_number"}


def validate_invoice(values: dict[str, object], duplicates: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    rules: list[dict[str, object]] = []
    required = ("supplier_name", "supplier_address", "supplier_tax_id", "customer_name", "customer_address", "invoice_number", "invoice_date")
    is_indian = values.get("currency") == "INR"
    tax_reference = "GST amount arithmetic only; GST classification/filing is not assessed" if is_indian else "UStG §14"
    gross_value = _decimal(values.get("gross_amount"))
    simplified = (
        not is_indian
        and values.get("currency") in (None, "", "EUR")
        and values.get("document_type", "invoice") == "invoice"
        and gross_value is not None
        and Decimal("0") < gross_value <= SIMPLIFIED_INVOICE_LIMIT
    )
    for field in required:
        detected = values.get(field)
        if simplified and field in SIMPLIFIED_EXEMPT_FIELDS and not detected:
            rules.append(
                _result(
                    f"INVOICE-{field.upper()}",
                    "PASS",
                    "low",
                    f"{field.replace('_', ' ').capitalize()} is not required on a small-amount invoice up to €250 gross.",
                    None,
                    None,
                    None,
                    "§33 UStDV (Kleinbetragsrechnung)",
                )
            )
            continue
        if field == "supplier_address" and detected:
            has_street = bool(
                re.search(
                    r"(?:straße|strasse|street|road|avenue|weg|platz)\b.*\d|\d+[a-z]?,?\s+[\w.-]+"
                    r"|\b(?:plot|house|flat|door|shop)\s*(?:no\.?\s*)?\d+",
                    str(detected),
                    re.IGNORECASE,
                )
            )
            if not has_street:
                detected = None
                explanation = "Supplier address was extracted, but a street address was not detected; review applicable invoice exceptions."
            else:
                explanation = f"{field.replace('_', ' ').capitalize()} was extracted; formal checks do not determine deductibility."
        else:
            explanation = f"{field.replace('_', ' ').capitalize()} {'was extracted' if detected else 'is missing'}; formal checks do not determine deductibility."
        rules.append(
            _result(
                f"INVOICE-{field.upper()}",
                "PASS" if detected else "NEEDS_REVIEW",
                "medium",
                explanation,
                detected,
                None,
                f"Provide {field.replace('_', ' ')}" if not detected else None,
                tax_reference,
            )
        )

    for party in ("supplier", "customer"):
        gstin = values.get(f"{party}_gstin")
        if gstin:
            details = gstin_details(str(gstin))
            valid_shape = bool(details["structurally_valid"])
            breakdown = (
                f"State {details['state_code']} ({details['state_name']}), PAN {details['pan']}, entity code {details['entity_code']}."
                if valid_shape
                else "Problems: " + "; ".join(map(str, details["problems"])) + "."
            )
            rules.append(
                _result(
                    f"IN-GSTIN-{party.upper()}-FORMAT",
                    "PASS" if valid_shape else "FAIL",
                    "medium" if valid_shape else "high",
                    ("GSTIN has the expected 15-character structure. " if valid_shape else "GSTIN does not match the expected structural format. ")
                    + breakdown
                    + " Registration status has not been verified.",
                    str(gstin),
                    None,
                    None if valid_shape else "Check the state code, PAN segment, entity code, 'Z' position and length against the supplier's GST certificate.",
                    "GSTIN structural format only (CGST Rules 2017, Rule 10); registration status not verified",
                )
            )
            if valid_shape:
                expected_check = gstin_check_character(str(details["gstin"]))
                checksum_ok = expected_check == str(details["gstin"])[-1]
                rules.append(
                    _result(
                        f"IN-GSTIN-{party.upper()}-CHECKSUM",
                        "PASS" if checksum_ok else "NEEDS_REVIEW",
                        "medium",
                        "GSTIN check character is consistent; registration status has not been verified."
                        if checksum_ok
                        else "GSTIN check character does not match the computed checksum. This often indicates a typing or extraction error; registration status has not been verified.",
                        str(details["gstin"])[-1],
                        expected_check,
                        None if checksum_ok else "Compare the GSTIN with the original document and verify it on the GST portal.",
                        "GSTIN checksum (mod-36) only; registration status not verified",
                    )
                )
        vat_id = values.get(f"{party}_vat_id")
        if vat_id:
            details = vat_id_details(str(vat_id))
            rules.append(
                _result(
                    f"VAT-ID-{party.upper()}-FORMAT",
                    "PASS" if details["format_valid"] else "FAIL",
                    "medium",
                    (f"VAT ID has a valid {details['country_code']} format." if details["format_valid"] else "VAT ID does not match any EU VAT-ID format.")
                    + " Format validation is not a registration check (EU VIES / BZSt).",
                    str(vat_id),
                    None,
                    None if details["format_valid"] else "Check the country prefix and number of digits.",
                    "UStG §14 Abs. 4 Nr. 2; §27a UStG (format only)",
                )
            )
            if details["checksum_valid"] is not None:
                rules.append(
                    _result(
                        f"VAT-ID-{party.upper()}-CHECKSUM",
                        "PASS" if details["checksum_valid"] else "NEEDS_REVIEW",
                        "medium",
                        "German VAT ID check digit is consistent (ISO 7064 MOD 11,10)."
                        if details["checksum_valid"]
                        else "German VAT ID check digit does not match; this usually indicates a typing or extraction error.",
                        str(vat_id),
                        None,
                        None if details["checksum_valid"] else "Compare the VAT ID with the original document and confirm it via BZSt/VIES.",
                        "§27a UStG; check digit only, registration not verified",
                    )
                )

    net = _decimal(values.get("net_amount"))
    rate = _decimal(values.get("vat_rate"))
    vat = _decimal(values.get("vat_amount"))
    gross = _decimal(values.get("gross_amount"))
    if net is not None and rate is not None and vat is not None:
        expected = (net * rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        passed = abs(expected - vat) <= Decimal("0.02")
        rules.append(
            _result(
                "IN-GST-ARITHMETIC" if is_indian else "DE-VAT-ARITHMETIC",
                "PASS" if passed else "FAIL",
                "high",
                "VAT amount matches net amount multiplied by the stated rate." if passed else "VAT amount does not match the stated net amount and rate.",
                str(vat),
                str(expected),
                f"Check the rate and consider correcting the VAT amount to {expected}.",
                tax_reference,
            )
        )
    else:
        rules.append(
            _result("IN-GST-ARITHMETIC" if is_indian else "DE-VAT-ARITHMETIC", "NEEDS_REVIEW", "medium", "Net amount, tax rate and tax amount are required for an arithmetic check.", str(values.get("vat_amount")) if values.get("vat_amount") is not None else None, None, None, tax_reference)
        )

    if net is not None and vat is not None and gross is not None:
        expected_gross = net + vat
        passed = abs(expected_gross - gross) <= Decimal("0.02")
        rules.append(
            _result(
                "IN-GST-GROSS-AMOUNT" if is_indian else "DE-GROSS-AMOUNT",
                "PASS" if passed else "FAIL",
                "high",
                "Gross amount matches net plus VAT." if passed else "Gross amount does not match net plus VAT.",
                str(gross),
                str(expected_gross),
                f"Check the totals and consider correcting the gross amount to {expected_gross}.",
                tax_reference,
            )
        )

    countries = {values.get("supplier_country"), values.get("customer_country")}
    indian_supplier = values.get("supplier_country") == "India" or (is_indian and not values.get("supplier_country"))
    german_party = "Germany" in countries or (not is_indian and not any(countries - {None, ""}))
    if indian_supplier:
        rules.extend(_gst_structure_rules(values))
    if german_party:
        if not is_indian:
            rules.extend(_german_invoice_rules(values, rate))
        rules.extend(_german_content_rules(values, vat, net))

    if duplicates is not None:
        rules.append(
            _result(
                "DUPLICATE-INVOICE",
                "FAIL" if duplicates else "PASS",
                "high" if duplicates else "low",
                (
                    "Possible duplicate: "
                    + "; ".join(f"{d.get('filename')} ({d.get('reason')})" for d in duplicates)
                    + ". Paying or deducting VAT twice must be avoided."
                )
                if duplicates
                else "No other invoice in this organization has the same supplier and invoice number or the same file.",
                values.get("invoice_number"),
                None,
                "Compare both documents; delete the duplicate or document why both are valid." if duplicates else None,
                "UStG §14 Abs. 4 Nr. 4 (unique invoice number); duplicate-payment control",
            )
        )

    if values.get("document_type") == "credit_note":
        reference = values.get("reference_invoice_number")
        rules.append(
            _result(
                "CREDIT-NOTE-REFERENCE",
                "PASS" if reference else "NEEDS_REVIEW",
                "medium",
                "Credit note references the original invoice." if reference else "Credit note does not reference an original invoice number.",
                reference,
                None,
                None if reference else "Add the number of the invoice being corrected.",
                "Formal reference check only; correction requirements need review",
            )
        )

    if values.get("transaction_direction"):
        missing_fx = values.get("exchange_rate_status") == "missing" or (
            values.get("currency") not in (None, "", "EUR") and not values.get("exchange_rate")
        )
        fx_explanation = (
            " No exchange rate, date or source was provided; conversion is not performed."
            if missing_fx
            else ""
        )
        rules.append(
            _result(
                "CROSS-BORDER-ADVISOR-REVIEW",
                "NEEDS_REVIEW",
                "high",
                "Cross-border transaction detected. Confirm party status, place of supply, goods/services treatment and applicable export/import conditions with an advisor; no reverse-charge conclusion was made."
                + fx_explanation,
                values.get("transaction_direction"),
                None,
                "Configure an exchange rate with its date and source before conversion; confirm transaction-specific tax facts with an advisor."
                if missing_fx
                else "Confirm transaction-specific tax facts with an advisor.",
                "Advisor review required; no legal determination",
            )
        )
    if values.get("reverse_charge_wording") or values.get("tax_type") == "reverse_charge_or_zero_rated_review":
        rules.append(
            _result(
                "REVERSE-CHARGE-CLAIM-REVIEW",
                "NEEDS_REVIEW",
                "high",
                "Reverse-charge or zero-rate wording was detected. The wording alone does not establish that the tax treatment applies.",
                values.get("reverse_charge_wording") or values.get("tax_type"),
                None,
                "Have an advisor verify the statutory conditions and required invoice wording.",
                "UStG §13b; applicability requires advisor review",
            )
        )
    return rules


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except InvalidOperation:
        return None


def _result(
    rule_id: str,
    result: str,
    severity: str,
    explanation: str,
    detected: object,
    expected: str | None,
    suggestion: str | None,
    legal_reference: str,
) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "result": result,
        "severity": severity,
        "explanation": explanation,
        "detected_value": detected,
        "expected_value": expected,
        "suggested_correction": suggestion,
        "legal_reference": legal_reference,
    }


GERMAN_VAT_RATES = {Decimal("0"), Decimal("7"), Decimal("19")}


def _state_code(value: object) -> str | None:
    match = re.search(r"\((\d{2})\)|^(\d{2})\b", str(value or "").strip())
    return (match.group(1) or match.group(2)) if match else None


def _gst_structure_rules(values: dict[str, object]) -> list[dict[str, object]]:
    rules: list[dict[str, object]] = []
    tax_type = values.get("tax_type")
    reference = "CGST/IGST Acts structure check only; place-of-supply determination requires review"
    if tax_type == "CGST + SGST":
        cgst, sgst = _decimal(values.get("cgst_rate")), _decimal(values.get("sgst_rate"))
        if cgst is not None and sgst is not None:
            equal = cgst == sgst
            rules.append(
                _result(
                    "IN-CGST-SGST-RATES",
                    "PASS" if equal else "FAIL",
                    "high",
                    "CGST and SGST rates are equal." if equal else "CGST and SGST rates differ; intra-state GST is normally split equally.",
                    f"CGST {cgst}% / SGST {sgst}%",
                    None,
                    None if equal else "Check both component rates against the document.",
                    reference,
                )
            )
    if tax_type not in ("CGST + SGST", "IGST"):
        return rules
    supplier_state = str(values.get("supplier_gstin") or "")[:2] or None
    supply_state = _state_code(values.get("place_of_supply"))
    if not supplier_state or not supply_state:
        rules.append(
            _result(
                "IN-GST-SUPPLY-TYPE",
                "NEEDS_REVIEW",
                "medium",
                "Supplier GSTIN state code or place-of-supply state code was not detected, so CGST/SGST vs. IGST could not be cross-checked.",
                tax_type,
                None,
                "Provide the supplier GSTIN and place of supply with its state code.",
                reference,
            )
        )
        return rules
    expected = "CGST + SGST" if supplier_state == supply_state else "IGST"
    consistent = tax_type == expected
    supply_kind = "intra-state" if expected == "CGST + SGST" else "inter-state"
    rules.append(
        _result(
            "IN-GST-SUPPLY-TYPE",
            "PASS" if consistent else "NEEDS_REVIEW",
            "high",
            f"Supplier state {supplier_state} and place of supply {supply_state} indicate an {supply_kind} supply; the tax type is consistent."
            if consistent
            else f"Supplier state {supplier_state} and place of supply {supply_state} indicate an {supply_kind} supply, which normally uses {expected}, but {tax_type} was charged.",
            tax_type,
            expected,
            None if consistent else "Confirm the place of supply and the GST components with an advisor.",
            reference,
        )
    )
    return rules


def _german_invoice_rules(values: dict[str, object], rate: Decimal | None) -> list[dict[str, object]]:
    rules: list[dict[str, object]] = []
    domestic = values.get("supplier_country") == "Germany" and values.get("customer_country") in (None, "Germany")
    if domestic and values.get("tax_type") == "VAT" and rate is not None:
        standard = abs(rate) in GERMAN_VAT_RATES
        rules.append(
            _result(
                "DE-VAT-RATE",
                "PASS" if standard else "NEEDS_REVIEW",
                "medium",
                f"{rate}% is a German VAT rate (0%, 7% or 19%)." if standard else f"{rate}% is not a current German VAT rate (0%, 7% or 19%).",
                str(rate),
                None,
                None if standard else "Check the stated rate and whether a special scheme applies.",
                "UStG §12",
            )
        )
    if values.get("document_type", "invoice") == "invoice":
        service_date = values.get("service_date")
        rules.append(
            _result(
                "DE-SERVICE-DATE",
                "PASS" if service_date else "NEEDS_REVIEW",
                "medium",
                "Date or period of supply was extracted." if service_date else "Date or period of supply (Leistungsdatum) was not detected; if it equals the invoice date, the invoice must still say so.",
                service_date,
                None,
                None if service_date else "Provide the date or period of supply.",
                "UStG §14 Abs. 4 Nr. 6",
            )
        )
    return rules


def _german_content_rules(values: dict[str, object], vat: Decimal | None, net: Decimal | None) -> list[dict[str, object]]:
    rules: list[dict[str, object]] = []
    has_description = bool(values.get("description")) or any(
        isinstance(item, dict) and item.get("description") for item in (values.get("line_items") or [])
    )
    rules.append(
        _result(
            "DE-DESCRIPTION",
            "PASS" if has_description else "NEEDS_REVIEW",
            "medium",
            "A description of the goods or services was extracted." if has_description else "No description of the goods or services (quantity and type) was detected.",
            values.get("description"),
            None,
            None if has_description else "Add the quantity and customary description of the goods or the type and scope of the service.",
            "UStG §14 Abs. 4 Nr. 5",
        )
    )
    zero_tax = vat is not None and vat == 0 and net is not None and net != 0
    if zero_tax:
        reason = values.get("tax_exemption_reference") or values.get("reverse_charge_wording")
        rules.append(
            _result(
                "DE-TAX-EXEMPTION-REFERENCE",
                "PASS" if reason else "NEEDS_REVIEW",
                "high",
                "The invoice states why no VAT is charged." if reason else "No VAT is charged but no exemption reference or reverse-charge statement was found.",
                reason,
                None,
                None if reason else "State the exemption (e.g. §4 UStG / Art. 146 VAT Directive) or 'Steuerschuldnerschaft des Leistungsempfängers'.",
                "UStG §14 Abs. 4 Nr. 8; §14a UStG",
            )
        )
    foreign_supplier_to_germany = (
        values.get("customer_country") == "Germany" and values.get("supplier_country") not in (None, "", "Germany")
    )
    if foreign_supplier_to_germany and (vat is None or vat == 0):
        wording = values.get("reverse_charge_wording")
        rules.append(
            _result(
                "DE-13B-WORDING",
                "PASS" if wording else "NEEDS_REVIEW",
                "medium",
                "Reverse-charge wording is present." if wording
                else "Invoice from a foreign supplier without VAT: if §13b UStG applies, the invoice should state 'Steuerschuldnerschaft des Leistungsempfängers'. The German recipient must self-assess VAT either way.",
                wording,
                "Steuerschuldnerschaft des Leistungsempfängers",
                None if wording else "Confirm §13b applicability with an advisor and request a corrected invoice if needed.",
                "UStG §13b Abs. 2 Nr. 1, Abs. 5; §14a Abs. 5 UStG",
            )
        )
    return rules

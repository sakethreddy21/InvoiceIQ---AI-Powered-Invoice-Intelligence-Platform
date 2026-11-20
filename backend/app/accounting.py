"""Booking suggestions from organization-defined account mappings.

Suggestions are proposals only: nothing is posted, and an accountant or admin must approve
each one before it can be exported. InvoiceIQ never invents account numbers or tax keys;
every account and key comes from the organization's own mapping configuration.
"""

from decimal import Decimal, InvalidOperation

from app.models import AccountMapping, Invoice, Organization


class SuggestionError(Exception):
    pass


def invoice_direction(values: dict[str, object], organization: Organization) -> str:
    """'outgoing' when the organization is the supplier (sales invoice), else 'incoming'."""
    identifiers = {str(item).strip().casefold() for item in organization.own_identifiers or [] if str(item).strip()}
    if not identifiers:
        return "incoming"
    supplier = {str(values.get(key) or "").strip().casefold() for key in ("supplier_name", "supplier_vat_id", "supplier_gstin", "supplier_tax_id")}
    customer = {str(values.get(key) or "").strip().casefold() for key in ("customer_name", "customer_vat_id", "customer_gstin", "customer_tax_id")}
    if identifiers & supplier:
        return "outgoing"
    if identifiers & customer:
        return "incoming"
    return "incoming"


def match_mapping(values: dict[str, object], mappings: list[AccountMapping], direction: str) -> tuple[AccountMapping, str, str]:
    description = " ".join(
        [str(values.get("description") or "")]
        + [str(item.get("description") or "") for item in values.get("line_items") or [] if isinstance(item, dict)]
    ).casefold()
    supplier = str(values.get("supplier_name") or "").casefold()
    candidates = sorted((m for m in mappings if m.active and m.direction == direction), key=lambda m: (m.priority, m.name))
    for field_name, haystack in (("description", description), ("supplier", supplier)):
        for mapping in candidates:
            for keyword in mapping.keywords:
                if keyword.strip() and keyword.strip().casefold() in haystack:
                    return mapping, keyword.strip(), field_name
    kind = "sales" if direction == "outgoing" else "purchase"
    raise SuggestionError(
        f"No {kind} account mapping matches this invoice's description or supplier. Add one in Accounting suggestions → Account mappings."
    )


def _money(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def build_booking(invoice: Invoice, mapping: AccountMapping, keyword: str, matched_on: str, direction: str) -> tuple[dict[str, object], str]:
    values = invoice.effective_data()
    failed = [r["rule_id"] for r in invoice.validation_results if r.get("result") == "FAIL"]
    if not invoice.validation_results:
        raise SuggestionError("Run the invoice checks before preparing a booking suggestion.")
    if failed:
        raise SuggestionError(f"Resolve failed checks first: {', '.join(map(str, failed))}.")
    currency = str(values.get("currency") or "EUR").upper()
    amounts = {key: _money(values.get(key)) for key in ("net_amount", "vat_amount", "gross_amount")}
    rate = None
    if currency != "EUR":
        conversion = invoice.fx_conversion or {}
        if conversion.get("from_currency") != currency:
            raise SuggestionError(f"Convert this {currency} invoice to EUR (with a documented rate) before booking.")
        converted = conversion.get("converted") or {}
        amounts = {key: _money(converted.get(key)) for key in amounts}
        rate = conversion.get("units_per_eur")
    gross = amounts["gross_amount"]
    if gross is None or gross == 0:
        raise SuggestionError("A non-zero gross amount is required for a booking suggestion.")
    if not values.get("invoice_date"):
        raise SuggestionError("An invoice date is required for a booking suggestion.")
    is_credit = gross < 0
    # DATEV convention: Konto is debited ("S") and Gegenkonto credited, reversed for credit notes.
    if direction == "outgoing":  # sales: debtor debited, revenue credited
        account, contra = mapping.creditor_account, mapping.expense_account
    else:  # purchase: expense debited, creditor credited
        account, contra = mapping.expense_account, mapping.creditor_account
    treatment = mapping.tax_treatment or (f"Tax key {mapping.tax_key} as configured" if mapping.tax_key else "No tax key: tax handled by the account setup")
    confidence = 0.85 if matched_on == "description" else 0.7
    if invoice.compliance_status == "NEEDS_REVIEW":
        confidence -= 0.1
    if currency != "EUR":
        confidence -= 0.05
    booking: dict[str, object] = {
        "direction": direction,
        "amount": str(abs(gross)),
        "debit_credit": "H" if is_credit else "S",
        "currency": "EUR",
        "net_amount": str(amounts["net_amount"]) if amounts["net_amount"] is not None else None,
        "vat_amount": str(amounts["vat_amount"]) if amounts["vat_amount"] is not None else None,
        "gross_amount": str(gross),
        "account": account,
        "contra_account": contra,
        "tax_key": mapping.tax_key,
        "tax_treatment": treatment,
        "document_date": values.get("invoice_date"),
        "document_number": values.get("invoice_number"),
        "text": str(values.get("supplier_name") if direction == "incoming" else values.get("customer_name") or values.get("description") or invoice.filename)[:60],
        "partner_name": values.get("supplier_name") if direction == "incoming" else values.get("customer_name"),
        "partner_vat_id": values.get("supplier_vat_id") if direction == "incoming" else values.get("customer_vat_id"),
        "original_currency": currency,
        "original_gross_amount": str(values.get("gross_amount")),
        "exchange_rate": rate,
        "confidence": round(confidence, 2),
        "mapping_is_demo": mapping.is_demo,
    }
    kind = "Sales" if direction == "outgoing" else "Purchase"
    explanation = (
        f"{kind} invoice. Mapping '{mapping.name}' matched keyword '{keyword}' in the {matched_on}. "
        f"Account {account} {'credited' if is_credit else 'debited'} against {contra}. Tax treatment: {treatment}."
    )
    if currency != "EUR":
        explanation += f" Converted from {currency} at {rate} per EUR ({(invoice.fx_conversion or {}).get('source')})."
    if mapping.is_demo:
        explanation += " Demo mapping — verify accounts and tax keys with your accountant."
    return booking, explanation

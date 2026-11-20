"""DATEV-style "Buchungsstapel" (EXTF 700, category 21, format version 13) CSV prototype.

Follows the publicly documented EXTF conventions: semicolon delimiter, Windows-1252
encoding, CRLF line endings, decimal comma, text fields in double quotes, Belegdatum as
DDMM with the year taken from the header period. Only the leading booking columns are
written. DATEV-ready prototype — real DATEV import compatibility has not yet been verified.
"""

from decimal import Decimal

import re
from datetime import date, datetime, timezone

from app.models import BookingSuggestion, Organization

COLUMNS = [
    "Umsatz (ohne Soll/Haben-Kz)",
    "Soll/Haben-Kennzeichen",
    "WKZ Umsatz",
    "Kurs",
    "Basis-Umsatz",
    "WKZ Basis-Umsatz",
    "Konto",
    "Gegenkonto (ohne BU-Schlüssel)",
    "BU-Schlüssel",
    "Belegdatum",
    "Belegfeld 1",
    "Belegfeld 2",
    "Skonto",
    "Buchungstext",
    "Postensperre",
    "Diverse Adressnummer",
    "Geschäftspartnerbank",
    "Sachverhalt",
    "Zinssperre",
    "Beleglink",
    "Beleginfo - Art 1",
    "Beleginfo - Inhalt 1",
    "Beleginfo - Art 2",
    "Beleginfo - Inhalt 2",
]
EXPORT_FILENAME = "EXTF_Buchungsstapel.csv"
PROTOTYPE_LABEL = "DATEV-ready prototype — real DATEV import compatibility has not yet been verified."
DOCUMENT_FIELD = re.compile(r"^[A-Za-z0-9$&%*+\-/]{1,36}$")


def organization_errors(organization: Organization) -> list[str]:
    errors = []
    if not re.fullmatch(r"\d{4,7}", organization.datev_consultant_number or "") or int(organization.datev_consultant_number or 0) < 1001:
        errors.append("Set a DATEV consultant number (Beraternummer, 1001–9999999) in Settings.")
    if not re.fullmatch(r"\d{1,5}", organization.datev_client_number or "") or int(organization.datev_client_number or 0) < 1:
        errors.append("Set a DATEV client number (Mandantennummer, 1–99999) in Settings.")
    if not 4 <= organization.account_length <= 8:
        errors.append("General-ledger account length must be between 4 and 8 digits.")
    return errors


def booking_errors(suggestion: BookingSuggestion, organization: Organization) -> list[str]:
    booking = suggestion.booking
    errors = []
    length = organization.account_length
    accounts = [str(booking.get("account") or ""), str(booking.get("contra_account") or "")]
    # Either side may be a personal account (debtor/creditor, one digit longer); the other must be a G/L account.
    for label, number in zip(("Account", "Contra account"), accounts):
        if not re.fullmatch(rf"\d{{{length},{length + 1}}}", number):
            errors.append(f"{label} must have {length} (G/L) or {length + 1} (personal account) digits.")
    if all(len(number) == length + 1 for number in accounts):
        errors.append(f"At least one side must be a {length}-digit general-ledger account.")
    if booking.get("tax_key") and not re.fullmatch(r"\d{1,4}", str(booking["tax_key"])):
        errors.append("Tax key (BU-Schlüssel) must be 1–4 digits.")
    if not DOCUMENT_FIELD.fullmatch(str(booking.get("document_number") or "")):
        errors.append("Document number (Belegfeld 1) must be 1–36 characters of A–Z, 0–9 or $&%*+-/.")
    try:
        date.fromisoformat(str(booking.get("document_date")))
    except ValueError:
        errors.append("Document date must be a valid ISO date.")
    if booking.get("currency") != "EUR":
        errors.append("Booking amounts must be in EUR (base currency).")
    if booking.get("original_currency") not in (None, "EUR") and not booking.get("exchange_rate"):
        errors.append("Foreign-currency booking without a recorded exchange rate.")
    if len(str(booking.get("text") or "")) > 60:
        errors.append("Booking text (Buchungstext) exceeds 60 characters.")
    return errors


def fiscal_year_start(organization: Organization, on: date) -> date:
    month, day = (int(part) for part in organization.fiscal_year_start.split("-"))
    start = date(on.year, month, day)
    return start if start <= on else date(on.year - 1, month, day)


def build_csv(organization: Organization, suggestions: list[BookingSuggestion]) -> tuple[str, str]:
    dates = [date.fromisoformat(str(s.booking["document_date"])) for s in suggestions]
    year_starts = {fiscal_year_start(organization, day) for day in dates}
    if len(year_starts) != 1:
        raise ValueError("A DATEV batch must contain bookings from a single fiscal year; export each year separately.")
    created = datetime.now(timezone.utc)
    header = [
        '"EXTF"', "700", "21", '"Buchungsstapel"', "13", created.strftime("%Y%m%d%H%M%S%f")[:17], "", '"IQ"',
        '"InvoiceIQ"', '""', organization.datev_consultant_number or "", organization.datev_client_number or "",
        year_starts.pop().strftime("%Y%m%d"), str(organization.account_length), min(dates).strftime("%Y%m%d"),
        max(dates).strftime("%Y%m%d"), '"InvoiceIQ prototype"', '""', "1", "0", "0", '"EUR"', "", '""', "", "",
        "", "", "", "", '"DATEV-ready prototype - import compatibility not verified"',
    ]
    lines = [";".join(header), ";".join(f'"{column}"' for column in COLUMNS)]
    for suggestion, day in zip(suggestions, dates):
        booking = suggestion.booking
        foreign = booking.get("original_currency") not in (None, "EUR") and booking.get("exchange_rate")
        if foreign:
            original = abs(Decimal(str(booking["original_gross_amount"])))
            amount, currency = _decimal_comma(original), str(booking["original_currency"])
            rate = _decimal_comma(Decimal(str(booking["exchange_rate"])), 6)
            base_amount, base_currency = _decimal_comma(Decimal(str(booking["amount"]))), "EUR"
        else:
            amount, currency, rate, base_amount, base_currency = _decimal_comma(Decimal(str(booking["amount"]))), "EUR", "", "", ""
        row = [
            amount,
            f'"{booking["debit_credit"]}"',
            f'"{currency}"',
            rate,
            base_amount,
            f'"{base_currency}"',
            str(booking["account"]),
            str(booking["contra_account"]),
            f'"{booking.get("tax_key") or ""}"',
            day.strftime("%d%m"),
            f'"{booking["document_number"]}"',
            '""',
            "",
            _text(booking.get("text"), 60),
            "", '""', "", "", "", '""',
            '"Geschäftspartner"' if booking.get("partner_name") else '""',
            _text(booking.get("partner_name"), 210),
            '"USt-IdNr."' if booking.get("partner_vat_id") else '""',
            _text(booking.get("partner_vat_id"), 210),
        ]
        lines.append(";".join(row))
    return "\r\n".join(lines) + "\r\n", EXPORT_FILENAME


def _decimal_comma(value: Decimal, places: int = 2) -> str:
    return f"{value:.{places}f}".replace(".", ",")


def _text(value: object, limit: int) -> str:
    return '"' + str(value or "")[:limit].replace('"', '""') + '"'

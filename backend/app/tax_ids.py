"""Tax identifier checks: GSTIN structure/checksum, EU VAT-ID format/checksum, and
registration-verification interfaces (VIES, BZSt). A format or checksum pass never
means the identifier is registered."""

import re
from dataclasses import dataclass

import httpx

from app.settings import settings

GSTIN_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
INDIAN_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "25": "Daman and Diu (pre-2020)", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh (pre-2014)", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}
PAN_HOLDER_TYPES = {
    "P": "Individual", "C": "Company", "H": "Hindu Undivided Family", "F": "Firm / LLP", "A": "Association of Persons",
    "T": "Trust", "B": "Body of Individuals", "L": "Local Authority", "J": "Artificial Juridical Person", "G": "Government",
}


def gstin_check_character(gstin: str) -> str:
    """Mod-36 check character over the first 14 characters of a GSTIN."""
    total = 0
    for index, character in enumerate(gstin[:14]):
        product = GSTIN_CHARSET.index(character) * (2 if index % 2 else 1)
        total += product // 36 + product % 36
    return GSTIN_CHARSET[(36 - total % 36) % 36]


def gstin_details(value: str) -> dict[str, object]:
    gstin = re.sub(r"\s", "", value or "").upper()
    problems: list[str] = []
    if len(gstin) != 15:
        problems.append(f"length is {len(gstin)}, expected 15 characters")
    state_code, pan, entity_code, z_char, check = gstin[:2], gstin[2:12], gstin[12:13], gstin[13:14], gstin[14:15]
    if state_code not in INDIAN_STATES:
        problems.append(f"state code '{state_code}' is not a valid GST state code")
    pan_valid = bool(re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", pan))
    if not pan_valid:
        problems.append(f"PAN segment '{pan}' does not match the PAN format (5 letters, 4 digits, 1 letter)")
    if not re.fullmatch(r"[1-9A-Z]", entity_code or ""):
        problems.append(f"entity code '{entity_code}' must be 1-9 or A-Z")
    if z_char != "Z":
        problems.append(f"14th character is '{z_char}', expected 'Z'")
    structural = not problems
    expected = gstin_check_character(gstin) if len(gstin) >= 14 and all(c in GSTIN_CHARSET for c in gstin[:14]) else None
    return {
        "gstin": gstin,
        "structurally_valid": structural,
        "problems": problems,
        "state_code": state_code or None,
        "state_name": INDIAN_STATES.get(state_code),
        "pan": pan or None,
        "pan_valid": pan_valid,
        "pan_holder_type": PAN_HOLDER_TYPES.get(pan[3:4]) if pan_valid else None,
        "entity_code": entity_code or None,
        "check_character": check or None,
        "expected_check_character": expected,
        "checksum_valid": bool(expected and structural and expected == check),
        "registration_status": "not_verified",
    }


EU_VAT_FORMATS = {
    "AT": r"U\d{8}", "BE": r"[01]\d{9}", "BG": r"\d{9,10}", "CY": r"\d{8}[A-Z]", "CZ": r"\d{8,10}", "DE": r"\d{9}",
    "DK": r"\d{8}", "EE": r"\d{9}", "EL": r"\d{9}", "ES": r"[A-Z0-9]\d{7}[A-Z0-9]", "FI": r"\d{8}", "FR": r"[A-HJ-NP-Z0-9]{2}\d{9}",
    "HR": r"\d{11}", "HU": r"\d{8}", "IE": r"\d[A-Z0-9+*]\d{5}[A-Z]{1,2}", "IT": r"\d{11}", "LT": r"\d{9}|\d{12}",
    "LU": r"\d{8}", "LV": r"\d{11}", "MT": r"\d{8}", "NL": r"\d{9}B\d{2}", "PL": r"\d{10}", "PT": r"\d{9}", "RO": r"\d{2,10}",
    "SE": r"\d{12}", "SI": r"\d{8}", "SK": r"\d{10}", "XI": r"\d{9}|\d{12}|GD\d{3}|HA\d{3}",
}


def german_vat_checksum_ok(digits: str) -> bool:
    """ISO 7064 MOD 11,10 check used for German USt-IdNr."""
    product = 10
    for character in digits[:8]:
        total = (int(character) + product) % 10 or 10
        product = (2 * total) % 11
    check = 11 - product
    return (0 if check == 10 else check) == int(digits[8])


def vat_id_details(value: str) -> dict[str, object]:
    vat_id = re.sub(r"[\s.-]", "", value or "").upper()
    country, number = vat_id[:2], vat_id[2:]
    pattern = EU_VAT_FORMATS.get(country)
    format_valid = bool(pattern and re.fullmatch(pattern, number))
    checksum: bool | None = None
    if format_valid and country == "DE":
        checksum = german_vat_checksum_ok(number)
    return {
        "vat_id": vat_id,
        "country_code": country if pattern else None,
        "format_valid": format_valid,
        "checksum_valid": checksum,
        "checksum_supported": country == "DE",
        "registration_status": "not_verified",
    }


@dataclass(frozen=True)
class RegistrationCheck:
    status: str  # valid | invalid | not_verified | unavailable
    provider: str
    detail: str
    name: str | None = None
    address: str | None = None


def verify_vat_registration(vat_id: str) -> RegistrationCheck:
    """EU VIES lookup (only when VIES_ENABLED=true). BZSt qualified confirmation requires
    the requester's own German VAT ID and is exposed as an interface only."""
    details = vat_id_details(vat_id)
    if not details["format_valid"]:
        return RegistrationCheck("invalid", "format", "The VAT ID format is not valid; no registry lookup was made.")
    if not settings.vies_enabled:
        return RegistrationCheck("not_verified", "none", "Registry verification is disabled (set VIES_ENABLED=true to query EU VIES).")
    try:
        response = httpx.post(
            settings.vies_url,
            json={"countryCode": details["country_code"], "vatNumber": str(details["vat_id"])[2:]},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return RegistrationCheck("unavailable", "vies", "EU VIES could not be reached; try again later.")
    if data.get("valid") is True:
        return RegistrationCheck("valid", "vies", "Registered according to EU VIES at the time of the query.", data.get("name"), data.get("address"))
    if data.get("valid") is False:
        return RegistrationCheck("invalid", "vies", "EU VIES reports this VAT ID as not valid.")
    return RegistrationCheck("unavailable", "vies", str(data.get("userError") or "VIES returned no result."))


def verify_bzst_qualified(vat_id: str, own_vat_id: str | None) -> RegistrationCheck:
    """Interface for the BZSt 'qualifizierte Bestätigungsabfrage' (eVatR). Not connected."""
    return RegistrationCheck(
        "not_verified",
        "bzst",
        "BZSt qualified confirmation is not connected. It requires your own German VAT ID and the eVatR interface.",
    )

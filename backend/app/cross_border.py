"""Deterministic, versioned, effective-dated India <-> Germany tax indicators.

Every output is an indicator for advisor review, never a legal determination. Rules only
use facts a person confirmed; values suggested from the document are shown separately.
"""

import re
from dataclasses import dataclass, field
from datetime import date

RULESET_VERSION = "2026.11.1"

FACT_QUESTIONS: dict[str, str] = {
    "supply_type": "Is this a supply of goods or of services?",
    "supplier_is_business": "Is the supplier acting as a business?",
    "customer_is_business": "Is the customer acting as a business (B2B)?",
    "supplier_registered": "Is the supplier registered for VAT/GST in its country?",
    "customer_registered": "Is the customer registered for VAT/GST in its country?",
    "place_of_supply": "Where is the place of supply stated or agreed?",
    "goods_physically_moved": "Were the goods physically transported between the countries?",
    "payment_in_convertible_foreign_exchange": "Is payment received in convertible foreign exchange (or INR where RBI permits)?",
    "distinct_persons": "Are supplier and recipient merely establishments of the same legal entity?",
    "export_evidence_available": "Is export evidence (customs/shipping documents) available?",
    "electronically_supplied": "Is the service electronically supplied (e.g. SaaS, downloads, streaming)?",
}

ALLOWED_FACT_VALUES: dict[str, set[str]] = {
    "supply_type": {"goods", "services"},
    "supplier_is_business": {"yes", "no"},
    "customer_is_business": {"yes", "no"},
    "supplier_registered": {"yes", "no"},
    "customer_registered": {"yes", "no"},
    "place_of_supply": {"germany", "india", "other"},
    "goods_physically_moved": {"yes", "no"},
    "payment_in_convertible_foreign_exchange": {"yes", "no"},
    "distinct_persons": {"yes", "no"},
    "export_evidence_available": {"yes", "no"},
    "electronically_supplied": {"yes", "no"},
}

DIRECTIONS = {
    "India_to_Germany": "India → Germany",
    "Germany_to_India": "Germany → India",
    "domestic_germany": "Domestic Germany",
    "domestic_india": "Domestic India",
    "other_international": "Other international",
    "unknown": "Unknown",
}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    direction: str
    supply_type: str  # goods | services | any
    jurisdiction: str
    valid_from: date
    required_facts: dict[str, str]  # fact -> required value, or "any" = must be answered
    treatment: str
    reasoning: str
    references: str
    actions: tuple[str, ...] = field(default_factory=tuple)
    valid_to: date | None = None
    version: str = RULESET_VERSION


RULES: tuple[Rule, ...] = (
    # India -> Germany, services
    Rule("IN-EXPORT-OF-SERVICES", "India_to_Germany", "services", "India", date(2017, 7, 1),
         {"payment_in_convertible_foreign_exchange": "yes", "distinct_persons": "no"},
         "Potential zero-rated export of services in India (under LUT/bond without IGST, or with IGST and refund).",
         "Supplier in India, recipient outside India and place of supply outside India are export-of-services conditions; payment in convertible foreign exchange and the parties not being establishments of one entity complete them.",
         "IGST Act 2017 §2(6), §13, §16",
         ("Confirm the Indian supplier has filed an LUT or will claim an IGST refund.", "Keep the bank realisation certificate (FIRC/BRC) for the payment.")),
    Rule("DE-REVERSE-CHARGE-FOREIGN-SERVICES", "India_to_Germany", "services", "Germany", date(2010, 1, 1),
         {"customer_is_business": "yes"},
         "Potential German reverse charge: the German business recipient owes the VAT.",
         "For B2B services the place of supply is where the recipient runs its business (Germany). A service by a supplier established abroad then shifts the tax liability to the recipient.",
         "UStG §3a Abs. 2; §13b Abs. 2 Nr. 1 and Abs. 5",
         ("Self-assess German VAT in the VAT return (Umsatzsteuer-Voranmeldung) and check input-VAT deduction.", "Check whether the invoice should state 'Steuerschuldnerschaft des Leistungsempfängers'.")),
    Rule("DE-B2C-FOREIGN-SERVICES", "India_to_Germany", "services", "Germany", date(2015, 1, 1),
         {"customer_is_business": "no", "electronically_supplied": "any"},
         "B2C service from abroad: generally not taxable in Germany, except electronically supplied, telecom and broadcasting services to German consumers.",
         "The general B2C rule places the supply where the supplier is established; electronic services to consumers are taxed where the consumer lives.",
         "UStG §3a Abs. 1 and Abs. 5",
         ("If the service is electronically supplied, check whether the supplier must register in Germany or use the non-Union OSS scheme.",)),
    # India -> Germany, goods
    Rule("IN-EXPORT-OF-GOODS", "India_to_Germany", "goods", "India", date(2017, 7, 1),
         {"goods_physically_moved": "yes", "export_evidence_available": "yes"},
         "Potential zero-rated export of goods in India.",
         "Goods leaving India for a place outside India are an export; zero rating needs shipping/customs evidence.",
         "IGST Act 2017 §2(5), §16",
         ("Keep the shipping bill and export evidence.",)),
    Rule("DE-IMPORT-VAT-GOODS", "India_to_Germany", "goods", "Germany", date(1980, 1, 1),
         {"goods_physically_moved": "yes"},
         "Import VAT (Einfuhrumsatzsteuer) and customs duty at the German border, not VAT on this invoice.",
         "Goods entering Germany from a third country are taxed on import by customs; the importer may deduct import VAT if entitled.",
         "UStG §1 Abs. 1 Nr. 4; §15 Abs. 1 Nr. 2 UStG",
         ("Obtain the customs import declaration showing the import VAT paid.", "Check customs duty and tariff classification with the customs broker.")),
    # Germany -> India, services
    Rule("DE-B2B-SERVICES-TO-THIRD-COUNTRY", "Germany_to_India", "services", "Germany", date(2010, 1, 1),
         {"customer_is_business": "yes"},
         "Potentially not taxable in Germany: place of supply at the Indian business recipient.",
         "For B2B services the place of supply is the recipient's place of business, which is outside Germany; the invoice should not show German VAT.",
         "UStG §3a Abs. 2",
         ("Keep evidence that the recipient is a business (e.g. GSTIN, registration certificate).", "State on the invoice why no German VAT is charged.")),
    Rule("DE-B2C-SERVICES-FROM-GERMANY", "Germany_to_India", "services", "Germany", date(2010, 1, 1),
         {"customer_is_business": "no"},
         "B2C service: generally taxable in Germany, except certain catalogue services to private recipients outside the EU.",
         "The general B2C rule places the supply where the supplier is established; consulting and similar services to private persons outside the EU are taxed at the recipient's residence.",
         "UStG §3a Abs. 1 and Abs. 4",
         ("Check whether the service falls under the §3a Abs. 4 catalogue (e.g. consulting, legal, data processing).",)),
    Rule("IN-IMPORT-OF-SERVICES-RCM", "Germany_to_India", "services", "India", date(2017, 7, 1),
         {"customer_is_business": "yes"},
         "Potential Indian reverse charge: the Indian business recipient pays IGST on the imported service.",
         "Import of services by a business recipient in India is taxed under reverse charge.",
         "IGST Act 2017 §5(3); Notification 10/2017-Integrated Tax (Rate)",
         ("The Indian recipient should pay IGST under reverse charge and may claim input tax credit.",)),
    # Germany -> India, goods
    Rule("DE-EXPORT-DELIVERY", "Germany_to_India", "goods", "Germany", date(1980, 1, 1),
         {"goods_physically_moved": "yes", "export_evidence_available": "yes"},
         "Potential tax-exempt export delivery from Germany.",
         "Goods transported to a third country can be exempt if the export is evidenced (Ausfuhrnachweis).",
         "UStG §4 Nr. 1 Buchst. a, §6; §§8-11 UStDV",
         ("Obtain the electronic export confirmation (Ausgangsvermerk/ATLAS).",)),
    Rule("IN-IMPORT-OF-GOODS", "Germany_to_India", "goods", "India", date(2017, 7, 1),
         {"goods_physically_moved": "yes"},
         "Customs duty and IGST on import into India.",
         "Goods imported into India bear IGST levied with customs duty.",
         "Customs Tariff Act 1975 §3(7); IGST Act 2017 §5(1)",
         ("The Indian importer files the bill of entry and pays IGST with customs duty.",)),
    # Domestic
    Rule("DE-DOMESTIC-SUPPLY", "domestic_germany", "any", "Germany", date(1980, 1, 1),
         {},
         "Domestic German supply: German VAT at 19% or 7%, unless an exemption or a domestic reverse-charge case (§13b Abs. 2 Nr. 4-11 UStG) applies.",
         "Supplier and customer are in Germany, so the supply is normally taxable in Germany.",
         "UStG §1 Abs. 1 Nr. 1, §12, §13b Abs. 2",
         ()),
    Rule("IN-DOMESTIC-SUPPLY", "domestic_india", "any", "India", date(2017, 7, 1),
         {},
         "Domestic Indian supply: CGST + SGST for intra-state supplies, IGST for inter-state supplies.",
         "Whether the supplier's state equals the place of supply decides intra-state or inter-state taxation.",
         "IGST Act 2017 §7, §8, §10, §12",
         ("Check that the place of supply matches the GST type charged.",)),
    Rule("OTHER-INTERNATIONAL", "other_international", "any", "Both", date(1980, 1, 1),
         {},
         "International transaction outside the India–Germany rule set.",
         "InvoiceIQ's rule set covers India–Germany transactions only.",
         "—",
         ("Have an advisor assess the place of supply for the countries involved.",)),
)

SERVICE_WORDS = r"service|consult|beratung|licen[cs]e|subscription|hosting|engineering|analytics|support|software|saas|design|dienstleistung|leistung"
GOODS_WORDS = r"goods|ware|material|hardware|equipment|supplies|bürobedarf|machine|maschine|parts|teile|product"


def classify(values: dict[str, object]) -> str:
    supplier, customer = values.get("supplier_country"), values.get("customer_country")
    if not supplier or not customer:
        if values.get("supplier_gstin") and values.get("customer_gstin"):
            return "domestic_india"
        return "unknown"
    if supplier == customer == "Germany":
        return "domestic_germany"
    if supplier == customer == "India":
        return "domestic_india"
    if supplier == "India" and customer == "Germany":
        return "India_to_Germany"
    if supplier == "Germany" and customer == "India":
        return "Germany_to_India"
    return "other_international"


def suggest_facts(values: dict[str, object]) -> dict[str, dict[str, str]]:
    """Facts suggested from the document. Never used by the rules until a person confirms them."""
    suggestions: dict[str, dict[str, str]] = {}
    text = " ".join(
        [str(values.get("description") or "")]
        + [str(item.get("description") or "") for item in values.get("line_items") or [] if isinstance(item, dict)]
    ).casefold()
    if re.search(SERVICE_WORDS, text):
        suggestions["supply_type"] = {"value": "services", "basis": "Description mentions a service."}
    elif re.search(GOODS_WORDS, text):
        suggestions["supply_type"] = {"value": "goods", "basis": "Description mentions goods."}
    for party in ("supplier", "customer"):
        if values.get(f"{party}_vat_id") or values.get(f"{party}_gstin"):
            suggestions[f"{party}_is_business"] = {"value": "yes", "basis": f"The {party} has a VAT ID or GSTIN on the invoice."}
            suggestions[f"{party}_registered"] = {"value": "yes", "basis": f"A {party} VAT ID/GSTIN is printed (registration not verified)."}
    place = str(values.get("place_of_supply") or "").casefold()
    if place:
        if "germany" in place or "deutschland" in place:
            suggestions["place_of_supply"] = {"value": "germany", "basis": "Place of supply printed on the invoice."}
        elif re.search(r"india|\(\d{2}\)", place):
            suggestions["place_of_supply"] = {"value": "india", "basis": "Place of supply printed on the invoice."}
    return suggestions


def assess(values: dict[str, object], facts: dict[str, object], field_meta: dict[str, object] | None = None) -> dict[str, object]:
    direction = classify(values)
    invoice_date = _parse_date(values.get("service_date")) or _parse_date(values.get("invoice_date"))
    supply_type = facts.get("supply_type")
    base = {
        "ruleset_version": RULESET_VERSION,
        "direction": direction,
        "direction_label": DIRECTIONS[direction],
        "transaction_type": supply_type if supply_type in ("goods", "services") else None,
        "evaluated_for_date": invoice_date.isoformat() if invoice_date else None,
        "advisor_review_required": True,
        "advisor_review_status": "pending_review",
        "suggested_facts": suggest_facts(values),
        "existing_wording": {
            "reverse_charge": values.get("reverse_charge_wording"),
            "tax_exemption": values.get("tax_exemption_reference"),
        },
        "message": "Indicators only. A tax advisor must confirm the treatment; InvoiceIQ does not make legal determinations.",
    }
    if direction == "unknown":
        return {**base, "status": "facts_missing", "confidence": "low", "indicators": [],
                "missing_facts": [{"fact": "countries", "question": "Set the supplier and customer countries to classify the transaction."}],
                "recommended_actions": ["Complete the supplier and customer countries in Extraction & Review."]}

    candidate_rules = [
        rule for rule in RULES
        if rule.direction == direction
        and (rule.supply_type == "any" or rule.supply_type == supply_type)
        and (invoice_date is None or (rule.valid_from <= invoice_date and (rule.valid_to is None or invoice_date <= rule.valid_to)))
    ]
    missing: list[dict[str, str]] = []
    needs_type = direction in ("India_to_Germany", "Germany_to_India")
    if needs_type and supply_type not in ("goods", "services"):
        missing.append({"fact": "supply_type", "question": FACT_QUESTIONS["supply_type"]})
    indicators: list[dict[str, object]] = []
    actions: list[str] = []
    for rule in candidate_rules:
        rule_missing = [fact for fact in rule.required_facts if facts.get(fact) not in ALLOWED_FACT_VALUES[fact]]
        unmet = [
            fact for fact, expected in rule.required_facts.items()
            if expected != "any" and facts.get(fact) in ALLOWED_FACT_VALUES[fact] and facts.get(fact) != expected
        ]
        if unmet:
            continue  # rule does not apply to these facts; it is not shown as a potential treatment
        for fact in rule_missing:
            if not any(item["fact"] == fact for item in missing):
                missing.append({"fact": fact, "question": FACT_QUESTIONS[fact]})
        status = "facts_missing" if rule_missing else "indicated"
        indicators.append({
            "rule_id": rule.rule_id,
            "version": rule.version,
            "jurisdiction": rule.jurisdiction,
            "status": status,
            "potential_treatment": rule.treatment,
            "reasoning": rule.reasoning,
            "missing_facts": rule_missing,
            "references": rule.references,
            "valid_from": rule.valid_from.isoformat(),
            "valid_to": rule.valid_to.isoformat() if rule.valid_to else None,
        })
        if status == "indicated":
            actions.extend(action for action in rule.actions if action not in actions)
    for item in missing:
        actions.insert(0, f"Answer: {item['question']}")
    if direction in ("India_to_Germany", "Germany_to_India") and not values.get("exchange_rate") and values.get("currency") not in (None, "EUR"):
        actions.append("Record an exchange rate with its date and source for the EUR amounts.")
    low_confidence_fields = [
        key for key in ("supplier_country", "customer_country")
        if isinstance((field_meta or {}).get(key), dict) and float((field_meta or {})[key].get("confidence", 1)) < 0.75
    ]
    confidence = "high" if not missing and not low_confidence_fields else "medium" if len(missing) <= 1 else "low"
    return {
        **base,
        "status": "facts_missing" if missing else "ready_for_advisor_review",
        "confidence": confidence,
        "confidence_basis": "Based on answered transaction facts and the extraction confidence of the party countries.",
        "missing_facts": missing,
        "indicators": indicators,
        "recommended_actions": actions,
    }


def rule_definitions() -> list[dict[str, object]]:
    return [
        {
            "rule_id": rule.rule_id, "version": rule.version, "jurisdiction": rule.jurisdiction, "direction": rule.direction,
            "supply_type": rule.supply_type, "valid_from": rule.valid_from, "valid_to": rule.valid_to,
            "definition": {"required_facts": rule.required_facts, "treatment": rule.treatment, "reasoning": rule.reasoning,
                           "references": rule.references, "actions": list(rule.actions)},
        }
        for rule in RULES
    ]


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None

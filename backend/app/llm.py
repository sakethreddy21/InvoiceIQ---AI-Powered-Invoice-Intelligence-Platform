"""Optional LLM-assisted extraction (configurable; off by default).

The LLM only proposes values for fields the deterministic extractors could not find.
Output is validated with Pydantic, tagged with source "llm" and a modest confidence,
and is never used to decide compliance. Set LLM_PROVIDER=anthropic and
ANTHROPIC_API_KEY to enable; invoice text is then sent to Anthropic's API.
"""

import logging

from pydantic import BaseModel, Field

from app.settings import settings

logger = logging.getLogger(__name__)
LLM_CONFIDENCE = 0.6
MAX_TEXT_CHARS = 40_000


class LlmLineItem(BaseModel):
    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    net_amount: str | None = None
    vat_rate: str | None = None


class LlmInvoiceFields(BaseModel):
    """Values copied from the document. Amounts as plain decimals, dates as YYYY-MM-DD."""

    supplier_name: str | None = None
    supplier_address: str | None = None
    supplier_country: str | None = None
    supplier_vat_id: str | None = None
    supplier_gstin: str | None = None
    customer_name: str | None = None
    customer_address: str | None = None
    customer_country: str | None = None
    customer_vat_id: str | None = None
    customer_gstin: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    service_date: str | None = None
    currency: str | None = Field(default=None, description="ISO 4217 code")
    net_amount: str | None = None
    vat_rate: str | None = None
    vat_amount: str | None = None
    gross_amount: str | None = None
    iban: str | None = None
    bic: str | None = None
    payment_terms: str | None = None
    reverse_charge_wording: str | None = Field(default=None, description="Exact wording, if present")
    tax_exemption_reference: str | None = Field(default=None, description="Exact wording, if present")
    line_items: list[LlmLineItem] = Field(default_factory=list)


PROMPT = (
    "Extract invoice fields from the document text below. Copy values exactly as they appear; "
    "do not infer, calculate or guess. Leave a field null when it is not stated. Normalise amounts "
    "to plain decimals with a dot (e.g. 1234.50, negative for credits) and dates to YYYY-MM-DD. "
    "Do not assess legal or tax compliance.\n\n<document>\n{text}\n</document>"
)


def llm_enabled() -> bool:
    return settings.llm_provider == "anthropic" and bool(settings.anthropic_api_key)


def extract_with_llm(text: str) -> tuple[dict[str, object], str | None]:
    """Returns (fields, warning). Never raises: failures fall back to rule-based extraction."""
    if not llm_enabled():
        return {}, None
    try:
        import anthropic
    except ImportError:
        return {}, "LLM extraction is configured but the anthropic package is not installed."
    if len(text) > MAX_TEXT_CHARS:
        return {}, "Document text is too long for LLM-assisted extraction; rule-based values only."
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=60)
    try:
        response = client.beta.messages.parse(
            model=settings.llm_model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": PROMPT.format(text=text)}],
            output_format=LlmInvoiceFields,
        )
    except anthropic.APIConnectionError:
        return {}, "The LLM provider could not be reached; rule-based values only."
    except anthropic.RateLimitError:
        return {}, "The LLM provider rate-limited the request; rule-based values only."
    except anthropic.APIStatusError as exc:
        logger.warning("LLM extraction failed with status %s", exc.status_code)
        return {}, "LLM-assisted extraction failed; rule-based values only."
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return {}, "The LLM did not return structured fields; rule-based values only."
    data = response.parsed_output.model_dump(exclude_none=True)
    data["line_items"] = [item for item in data.get("line_items", []) if any(item.values())]
    return {key: value for key, value in data.items() if value not in ("", [], None)}, None

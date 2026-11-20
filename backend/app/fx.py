"""Currency conversion to EUR with explicit rate provenance."""

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
from lxml import etree

from app.settings import settings

ECB_NAMESPACE = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
_cache: dict[str, tuple[float, dict[str, dict[str, Decimal]]]] = {}
CACHE_SECONDS = 6 * 60 * 60


class FxError(Exception):
    pass


@dataclass(frozen=True)
class Rate:
    currency: str
    units_per_eur: Decimal  # 1 EUR = units_per_eur * currency
    rate_date: date
    source: str
    provider: str


def manual_rate(currency: str, units_per_eur: Decimal, rate_date: date, source: str) -> Rate:
    if units_per_eur <= 0:
        raise FxError("The exchange rate must be positive")
    if not source.strip():
        raise FxError("State where the manual exchange rate comes from")
    return Rate(currency, units_per_eur, rate_date, source.strip(), "manual")


SUPPORTED_CURRENCIES = ("EUR", "INR", "USD", "GBP")
# Fictional, fixed sample rates for offline demos. Always labelled as demo data.
DEMO_UNITS_PER_EUR = {"INR": Decimal("90.0000"), "USD": Decimal("1.0800"), "GBP": Decimal("0.8500")}
DEMO_SOURCE = "DEMO RATE — fixed sample value for demonstrations only, not a market rate"


def demo_rate(currency: str, on: date) -> Rate:
    if currency not in DEMO_UNITS_PER_EUR:
        raise FxError(f"No demo rate is defined for {currency}")
    return Rate(currency, DEMO_UNITS_PER_EUR[currency], on, DEMO_SOURCE, "demo")


def ecb_rate(currency: str, on: date) -> Rate:
    """ECB euro reference rate for the given date, or the last publication before it."""
    if on > date.today():
        raise FxError("ECB rates are not available for future dates")
    url = settings.ecb_rates_90d_url if (date.today() - on).days < 85 else settings.ecb_rates_url
    table = _ecb_table(url)
    for offset in range(0, 8):
        day = (on - timedelta(days=offset)).isoformat()
        if day in table and currency in table[day]:
            return Rate(
                currency,
                table[day][currency],
                date.fromisoformat(day),
                f"ECB euro foreign exchange reference rate ({url})",
                "ecb",
            )
    raise FxError(f"No ECB reference rate for {currency} on or within 7 days before {on.isoformat()}")


def fetch_ecb_xml(url: str) -> bytes:
    try:
        response = httpx.get(url, timeout=15)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FxError("The ECB rate service could not be reached; enter a rate manually with its source") from exc
    return response.content


def _ecb_table(url: str) -> dict[str, dict[str, Decimal]]:
    cached = _cache.get(url)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(fetch_ecb_xml(url), parser)
    table: dict[str, dict[str, Decimal]] = {}
    for day in root.iterfind(".//e:Cube[@time]", ECB_NAMESPACE):
        table[day.get("time")] = {
            cube.get("currency"): Decimal(cube.get("rate")) for cube in day.iterfind("e:Cube", ECB_NAMESPACE)
        }
    if not table:
        raise FxError("The ECB response did not contain any rates")
    _cache[url] = (time.monotonic(), table)
    return table


def store_rate(db, rate: Rate) -> None:
    """Persist the rate used, so every conversion has a recorded source and date."""
    from sqlalchemy import select

    from app.models import ExchangeRate

    existing = db.scalar(
        select(ExchangeRate).where(
            ExchangeRate.provider == rate.provider, ExchangeRate.currency == rate.currency, ExchangeRate.rate_date == rate.rate_date
        )
    )
    if existing is None:
        db.add(ExchangeRate(provider=rate.provider, currency=rate.currency, rate_date=rate.rate_date, units_per_eur=rate.units_per_eur, source=rate.source))


def convert(values: dict[str, object], rate: Rate) -> dict[str, object]:
    converted: dict[str, str | None] = {}
    for key in ("net_amount", "vat_amount", "gross_amount"):
        raw = values.get(key)
        try:
            amount = Decimal(str(raw)) if raw not in (None, "") else None
        except ArithmeticError:
            amount = None
        converted[key] = (
            str((amount / rate.units_per_eur).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if amount is not None else None
        )
    return {
        "from_currency": rate.currency,
        "to_currency": "EUR",
        "units_per_eur": str(rate.units_per_eur),
        "rate_date": rate.rate_date.isoformat(),
        "source": rate.source,
        "provider": rate.provider,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "original_amounts": {key: values.get(key) for key in ("net_amount", "vat_amount", "gross_amount")},
        "converted": converted,
        "is_demo_rate": rate.provider == "demo",
        "note": "Check that this rate is acceptable for VAT purposes (UStG §16 Abs. 6) with your advisor.",
    }

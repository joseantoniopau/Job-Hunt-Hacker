"""Salary text parser.

Handles common patterns:
  $120k-$160k         -> 120000-160000 USD/year
  120,000 - 160,000   -> 120000-160000
  $75/hr              -> annualized at 2080
  €50k                -> 50000 EUR/year
  £60,000             -> 60000 GBP/year
  $50-60 per hour     -> hourly -> annual
  130k base + equity  -> 130000 USD/year

Returns {min, max, currency, period} with ints for min/max when known,
None when unparseable.
"""
from __future__ import annotations

import re
from typing import Optional

HOURS_PER_YEAR = 2080

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₽": "RUB",
    "₩": "KRW",
    "C$": "CAD",
    "A$": "AUD",
}

_CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY", "INR", "RUB", "KRW", "CAD", "AUD", "CHF", "CNY", "MXN", "BRL", "SEK", "NOK", "DKK"}

# Period detection
_HOURLY_RX = re.compile(r"\b(per\s*hour|hourly|/\s*hr|/\s*hour|an\s*hour|hr\.)\b", re.I)
_MONTHLY_RX = re.compile(r"\b(per\s*month|monthly|/\s*mo|/\s*month|a\s*month|mo\.)\b", re.I)
_YEARLY_RX = re.compile(r"\b(per\s*year|annual|annually|/\s*yr|/\s*year|a\s*year|yr\.|p\.?a\.?)\b", re.I)

# Number pattern: handles $120k, 120,000, 120.5k, 75
_NUM_RX = re.compile(
    r"""(?ix)
    (?P<sym>C\$|A\$|\$|€|£|¥|₹|₽|₩)?\s*
    (?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)
    \s*
    (?P<suffix>k|m|thousand|million)?
    """,
)


def _to_float(num: str, suffix: str | None) -> float:
    n = float(num.replace(",", ""))
    s = (suffix or "").lower()
    if s in ("k", "thousand"):
        n *= 1_000
    elif s in ("m", "million"):
        n *= 1_000_000
    return n


def _detect_currency(text: str, hint: str = "USD") -> tuple[str, str]:
    """Return (currency_code, currency_symbol_found_or_blank)."""
    if not text:
        return hint, ""
    # symbols
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in text:
            return code, sym
    # codes (whole word)
    upper = text.upper()
    for code in _CURRENCY_CODES:
        if re.search(rf"\b{re.escape(code)}\b", upper):
            return code, ""
    return hint, ""


def _detect_period(text: str) -> Optional[str]:
    if _HOURLY_RX.search(text):
        return "hour"
    if _MONTHLY_RX.search(text):
        return "month"
    if _YEARLY_RX.search(text):
        return "year"
    return None


def parse_salary(text: str, currency_hint: str = "USD") -> dict:
    """Parse a salary string. Always returns a dict with keys
    min, max, currency, period (period may be None).
    """
    empty = {"min": None, "max": None, "currency": currency_hint, "period": None}
    if not text:
        return empty

    s = str(text).strip()
    currency, _ = _detect_currency(s, currency_hint)
    period_hint = _detect_period(s)

    matches = list(_NUM_RX.finditer(s))
    # filter out matches that look like years (4-digit, no symbol, no suffix, between 1900-2100)
    nums: list[float] = []
    saw_currency = False
    saw_suffix = False
    for m in matches:
        raw_num = m.group("num")
        suffix = m.group("suffix")
        sym = m.group("sym")
        try:
            n = _to_float(raw_num, suffix)
        except ValueError:
            continue
        # skip year-like (1900-2100) without currency / suffix
        if not sym and not suffix and "," not in raw_num and "." not in raw_num:
            if 1900 <= n <= 2100 and len(raw_num) == 4:
                continue
        # tiny values without suffix probably not salary
        if not suffix and not sym and n < 1000 and period_hint != "hour":
            # allow small hourly rates if hourly period detected
            continue
        nums.append(n)
        if sym:
            saw_currency = True
        if suffix:
            saw_suffix = True

    if not nums:
        return {"min": None, "max": None, "currency": currency, "period": period_hint}

    lo = min(nums)
    hi = max(nums)

    # If we have only one number, mn==mx
    if len(nums) == 1:
        lo = hi = nums[0]

    # Determine period if not explicit:
    period = period_hint
    if period is None:
        # heuristic: if values <= 500 and no k/m suffix, treat as hourly
        if hi <= 500 and not saw_suffix:
            period = "hour"
        else:
            period = "year"

    # Normalize hourly -> annual
    if period == "hour":
        lo = lo * HOURS_PER_YEAR
        hi = hi * HOURS_PER_YEAR
    elif period == "month":
        # keep period for clarity but expose annualized min/max
        lo = lo * 12
        hi = hi * 12

    return {
        "min": int(round(lo)),
        "max": int(round(hi)),
        "currency": currency,
        "period": period,
    }

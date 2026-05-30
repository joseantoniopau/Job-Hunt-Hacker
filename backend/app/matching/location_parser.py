"""Location parsing + matching.

parse_location("Remote - US only") -> {city: None, region: None, country: "US",
                                       remote: True, hybrid: False}
parse_location("San Francisco, CA, USA")
match_location(job_loc, user_prefs) -> float 0..1
"""
from __future__ import annotations

import re
from typing import Optional

_REMOTE_RX = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed|fully remote|100%\s*remote)\b", re.I)
_HYBRID_RX = re.compile(r"\b(hybrid|flex(?:ible)?\s*(?:work|location))\b", re.I)
_ONSITE_RX = re.compile(r"\b(on[- ]?site|in[- ]?office|in[- ]?person)\b", re.I)
_US_ONLY_RX = re.compile(r"\b(us only|usa only|u\.s\. only|united states only|usa-only)\b", re.I)

_COUNTRY_ALIASES = {
    "USA": "US", "U.S.": "US", "U.S.A.": "US", "UNITED STATES": "US", "AMERICA": "US",
    "UK": "GB", "U.K.": "GB", "BRITAIN": "GB", "ENGLAND": "GB", "UNITED KINGDOM": "GB",
    "DEUTSCHLAND": "DE", "GERMANY": "DE",
    "ESPAÑA": "ES", "SPAIN": "ES",
    "FRANCE": "FR",
    "CANADA": "CA",
    "AUSTRALIA": "AU",
    "INDIA": "IN",
    "JAPAN": "JP",
    "BRASIL": "BR", "BRAZIL": "BR",
    "MEXICO": "MX", "MÉXICO": "MX",
}

# A small US state set (abbrev). Used to identify region tokens.
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def _norm_country_token(tok: str) -> Optional[str]:
    t = tok.strip().upper()
    if not t:
        return None
    if t in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[t]
    if len(t) == 2 and t.isalpha():
        # could be country code or state — only treat as country if not US state
        if t in _US_STATES:
            return None
        return t
    return None


def parse_location(text: str) -> dict:
    out = {"city": None, "region": None, "country": None, "remote": False, "hybrid": False}
    if not text:
        return out

    s = str(text).strip()
    if _REMOTE_RX.search(s):
        out["remote"] = True
    if _HYBRID_RX.search(s):
        out["hybrid"] = True
    if _US_ONLY_RX.search(s):
        out["country"] = "US"

    # tokenize "City, Region, Country"
    parts = [p.strip() for p in re.split(r"[,/|·•·]", s) if p.strip()]
    # remove explicit remote/hybrid tokens from parts
    parts = [p for p in parts if not _REMOTE_RX.fullmatch(p) and not _HYBRID_RX.fullmatch(p)]

    if parts:
        # last token might be country
        last = parts[-1]
        cc = _norm_country_token(last)
        if cc:
            out["country"] = cc
            parts = parts[:-1]

        # next-to-last might be region/state
        if parts:
            tail = parts[-1].upper().strip()
            if tail in _US_STATES:
                out["region"] = tail
                if not out["country"]:
                    out["country"] = "US"
                parts = parts[:-1]
            elif len(tail) <= 3 and tail.isalpha():
                out["region"] = tail
                parts = parts[:-1]
            elif len(parts) >= 2:
                # treat as region only if not the city
                out["region"] = parts[-1]
                parts = parts[:-1]

        if parts:
            out["city"] = parts[0]

    return out


def _country_match(job_country: Optional[str], user_country: Optional[str]) -> float:
    if not job_country or not user_country:
        return 0.6  # neutral / unknown
    return 1.0 if job_country.upper() == user_country.upper() else 0.0


def match_location(job_loc: dict, user_prefs: dict) -> float:
    """Score 0..1 based on remote pref, country/region match, and preferred cities.

    user_prefs schema (best-effort):
      {
        "remote_preference": "remote" | "hybrid" | "onsite" | "any" | None,
        "preferred_locations": ["San Francisco, CA", "Remote", "Berlin, DE"],
        "location": "Austin, TX"   # user's own home location
      }
    """
    job_loc = job_loc or {}
    user_prefs = user_prefs or {}

    pref = (user_prefs.get("remote_preference") or "").lower().strip()
    preferred = [str(x) for x in (user_prefs.get("preferred_locations") or [])]
    user_home = user_prefs.get("location") or ""
    user_home_parsed = parse_location(user_home) if user_home else {}

    # Remote handling
    if job_loc.get("remote"):
        if pref in ("remote", "any", ""):
            return 1.0
        if pref == "hybrid":
            return 0.7
        if pref == "onsite":
            return 0.4
        return 0.85

    if job_loc.get("hybrid"):
        if pref in ("hybrid", "any", ""):
            return 0.9
        if pref == "remote":
            return 0.55
        if pref == "onsite":
            return 0.7
        return 0.75

    # On-site path: compare countries / cities
    score = 0.0
    matched = False

    # Preferred locations text match
    for p in preferred:
        pl = parse_location(p)
        if pl.get("remote") and job_loc.get("remote"):
            return 1.0
        city_match = pl.get("city") and job_loc.get("city") and \
            pl["city"].lower() == job_loc["city"].lower()
        region_match = pl.get("region") and job_loc.get("region") and \
            pl["region"].upper() == job_loc["region"].upper()
        country_match = pl.get("country") and job_loc.get("country") and \
            pl["country"].upper() == job_loc["country"].upper()
        if city_match:
            return 1.0
        if region_match and country_match:
            score = max(score, 0.85)
            matched = True
        elif country_match:
            score = max(score, 0.7)
            matched = True

    # Same country as user home
    if not matched and user_home_parsed:
        cscore = _country_match(job_loc.get("country"), user_home_parsed.get("country"))
        score = max(score, cscore * 0.65)

    # If we still have nothing usable, give neutral
    if score == 0.0 and not job_loc.get("country") and not job_loc.get("city"):
        return 0.5
    if score == 0.0:
        # onsite job, no overlap
        if pref == "remote":
            return 0.2
        return 0.35

    return min(1.0, score)

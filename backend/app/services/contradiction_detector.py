"""Scan career_claim for likely contradictions and flag them.

Detected patterns:
  - same employer with overlapping date ranges (date hygiene)
  - role claims with different employers in the same period (overlap)
  - degree claimed multiple times from different schools
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..db import get_conn, row_to_dict
from . import career_vault

log = logging.getLogger("jhh.evidence")

MONTHS_IDX = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(token: str | None) -> tuple[int, int] | None:
    """Return (year, month) tuple or None."""
    if not token:
        return None
    s = token.strip().lower()
    if s in ("present", "current", "now"):
        return (9999, 12)
    m = re.match(r"(\d{1,2})/(\d{4})", s)
    if m:
        return (int(m.group(2)), int(m.group(1)))
    m = re.match(r"([a-z]{3,9})\.?\s+(\d{4})", s)
    if m and m.group(1)[:4] in MONTHS_IDX:
        return (int(m.group(2)), MONTHS_IDX[m.group(1)[:4]])
    m = re.match(r"([a-z]{3,9})\.?\s+(\d{4})", s)
    if m:
        mo = MONTHS_IDX.get(m.group(1)[:3])
        if mo:
            return (int(m.group(2)), mo)
    m = re.match(r"^(\d{4})$", s)
    if m:
        return (int(m.group(1)), 1)
    return None


def _range(claim: dict) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    return _parse_date(claim.get("date_start")), _parse_date(claim.get("date_end"))


def _overlap(a: tuple[tuple[int, int] | None, tuple[int, int] | None],
             b: tuple[tuple[int, int] | None, tuple[int, int] | None]) -> bool:
    a0, a1 = a
    b0, b1 = b
    if not a0 or not b0:
        return False
    if not a1:
        a1 = (9999, 12)
    if not b1:
        b1 = (9999, 12)
    return not (a1 < b0 or b1 < a0)


def _norm_employer(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9 ]+", "", name.lower())
    s = re.sub(r"\b(inc|llc|ltd|co|corp|corporation|company|gmbh|sa|sas)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def find_contradictions() -> list[dict[str, Any]]:
    conn = get_conn()
    claims = [row_to_dict(r) for r in
              conn.execute("SELECT * FROM career_claim").fetchall()]

    findings: list[dict[str, Any]] = []
    involved_ids: set[int] = set()

    role_claims = [c for c in claims if c["claim_type"] == "role"]

    # 1) same employer overlapping ranges
    by_emp: dict[str, list[dict]] = {}
    for c in role_claims:
        key = _norm_employer(c.get("employer"))
        if not key:
            continue
        by_emp.setdefault(key, []).append(c)

    for emp, group in by_emp.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if _overlap(_range(a), _range(b)):
                    # If their text is essentially identical, that's a dup not
                    # a contradiction.
                    if (a.get("normalized_claim") or "")[:80] == \
                       (b.get("normalized_claim") or "")[:80]:
                        continue
                    findings.append({
                        "type": "overlapping_same_employer",
                        "claim_ids": [a["id"], b["id"]],
                        "explanation": (f"Two role claims at the same employer "
                                        f"({emp}) overlap in time."),
                    })
                    involved_ids.update([a["id"], b["id"]])

    # 2) different employers in the same period
    dated = [c for c in role_claims if _parse_date(c.get("date_start"))]
    for i in range(len(dated)):
        for j in range(i + 1, len(dated)):
            a, b = dated[i], dated[j]
            ea, eb = _norm_employer(a.get("employer")), _norm_employer(b.get("employer"))
            if not ea or not eb or ea == eb:
                continue
            if _overlap(_range(a), _range(b)):
                findings.append({
                    "type": "overlapping_different_employers",
                    "claim_ids": [a["id"], b["id"]],
                    "explanation": (f"Roles at different employers ({ea} vs "
                                    f"{eb}) overlap in time."),
                })
                involved_ids.update([a["id"], b["id"]])

    # 3) degree claimed from multiple schools
    degree_claims = [c for c in claims if c["claim_type"] == "degree"]
    school_re = re.compile(r"\b(?:at|from)\s+([A-Z][A-Za-z .'&\-]{2,60})")
    schools: dict[str, str] = {}  # claim_id -> school
    for c in degree_claims:
        m = school_re.search(c.get("claim_text") or "")
        if m:
            schools[str(c["id"])] = m.group(1).strip().lower()

    # group by degree level
    level_re = re.compile(r"\b(bachelor|master|mba|ph\.?d|phd|doctorate)\b", re.I)
    by_level: dict[str, list[dict]] = {}
    for c in degree_claims:
        m = level_re.search(c.get("claim_text") or "")
        if not m:
            continue
        by_level.setdefault(m.group(1).lower().replace(".", ""), []).append(c)

    for level, group in by_level.items():
        seen_schools = {}
        for c in group:
            sch = schools.get(str(c["id"]))
            if not sch:
                continue
            if sch in seen_schools:
                continue
            seen_schools[sch] = c
        if len(seen_schools) > 1:
            ids = [c["id"] for c in seen_schools.values()]
            findings.append({
                "type": "duplicate_degree_different_schools",
                "claim_ids": ids,
                "explanation": (f"Multiple '{level}' degrees claimed from "
                                f"different schools: {sorted(seen_schools.keys())}"),
            })
            involved_ids.update(ids)

    # Persist suspected status
    for cid in involved_ids:
        try:
            career_vault.update_claim(int(cid), {"contradiction_status": "suspected"})
        except Exception as e:  # noqa: BLE001
            log.warning("could not mark claim %d as suspected: %s", cid, e)

    return findings

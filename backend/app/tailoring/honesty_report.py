"""Build the honesty report attached to every tailored output."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .provenance import ProvenanceMap


def _risk_from_coverage(coverage: dict, n_dropped: int, n_unsupported_kw: int) -> str:
    n_seg = coverage.get("n_segments", 0) or 0
    n_with = coverage.get("n_with_evidence", 0) or 0
    if n_seg == 0:
        ratio = 1.0
    else:
        ratio = n_with / n_seg
    if n_dropped >= 3 or n_unsupported_kw >= 5 or ratio < 0.6:
        return "high"
    if n_dropped >= 1 or n_unsupported_kw >= 2 or ratio < 0.85:
        return "medium"
    return "low"


def _recommendation(risk: str, n_dropped: int, gaps: list[str]) -> str:
    if risk == "high":
        return (
            "High risk of overstatement. Review the dropped segments and missing "
            "evidence before submitting. Consider adding evidence to your vault, "
            "or accept the gap report and submit as-is."
        )
    if risk == "medium":
        return (
            "Medium risk. A few segments lacked solid evidence — they have been "
            "removed. Review the gaps list and decide whether to add evidence."
        )
    return (
        "Low risk. Every shipped segment is grounded in your evidence vault. "
        "Safe to submit. Review the gaps list for follow-ups."
    )


def build_report(
    provenance: ProvenanceMap,
    keyword_matrix: list[dict] | None,
    gaps_flagged: list[str] | None,
    dropped_segments: list[dict] | None,
    *,
    keywords_added: list[str] | None = None,
    keywords_excluded_as_unsupported: list[str] | None = None,
    unsupported_job_requirements: list[str] | None = None,
    wording_changed: list[dict] | None = None,
    missing_evidence: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate everything we know about the honesty of an output."""
    coverage = provenance.coverage()
    all_ids = list(provenance.all_ids())

    # facts emphasized = count how many segments reference each id
    counter: Counter[int] = Counter()
    for ids in (provenance._map.values() if hasattr(provenance, "_map") else []):
        for i in ids:
            counter[i] += 1
    facts_emphasized = [
        {"evidence_id": int(eid), "count": int(c)}
        for eid, c in counter.most_common()
    ]

    dropped = list(dropped_segments or [])
    gaps = list(gaps_flagged or [])
    added = list(keywords_added or [])
    excluded = list(keywords_excluded_as_unsupported or [])
    unsup_reqs = list(unsupported_job_requirements or [])
    wording = list(wording_changed or [])
    missing = list(missing_evidence or [])

    risk = _risk_from_coverage(coverage, len(dropped), len(excluded))

    return {
        "facts_used": len(all_ids),
        "facts_emphasized": facts_emphasized,
        "wording_changed": wording,
        "keywords_added": added,
        "keywords_excluded_as_unsupported": excluded,
        "unsupported_job_requirements": unsup_reqs,
        "potential_overstatement_risk": risk,
        "missing_evidence": missing,
        "gaps_flagged": gaps,
        "recommendation": _recommendation(risk, len(dropped), gaps),
        "dropped_segments": dropped,
        "provenance_coverage": coverage,
    }

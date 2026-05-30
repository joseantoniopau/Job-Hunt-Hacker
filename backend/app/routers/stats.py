"""GET /api/stats — summary counts for the dashboard."""
from __future__ import annotations

from fastapi import APIRouter

from ..db import get_conn

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
def stats() -> dict:
    c = get_conn()

    def cnt(sql: str, *p) -> int:
        r = c.execute(sql, p).fetchone()
        return int(r[0]) if r else 0

    pipeline = {}
    for row in c.execute(
        "SELECT status, COUNT(*) FROM application GROUP BY status"
    ).fetchall():
        pipeline[row[0]] = int(row[1])

    return {
        "ok": True,
        "data": {
            "evidence_sources": cnt("SELECT COUNT(*) FROM evidence_source"),
            "career_claims": cnt("SELECT COUNT(*) FROM career_claim"),
            "verified_claims": cnt("SELECT COUNT(*) FROM career_claim WHERE user_verified = 1"),
            "resume_documents": cnt("SELECT COUNT(*) FROM resume_document"),
            "jobs_discovered": cnt("SELECT COUNT(*) FROM job_posting"),
            "jobs_scored": cnt("SELECT COUNT(*) FROM job_match"),
            "tailored_resumes": cnt("SELECT COUNT(*) FROM tailored_resume"),
            "cover_letters": cnt("SELECT COUNT(*) FROM cover_letter"),
            "applications": cnt("SELECT COUNT(*) FROM application"),
            "saved_searches": cnt("SELECT COUNT(*) FROM saved_search WHERE enabled = 1"),
            "pipeline": pipeline,
        },
    }

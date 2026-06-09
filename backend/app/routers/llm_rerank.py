"""POST/GET /api/scoring/llm-rerank — second-pass semantic scoring.

Thin HTTP shell around `backend.app.matching.llm_rerank`. The heavy
prompt construction + persistence lives there; this router just maps
HTTP shapes to its public functions and surfaces an API for the UI to
list LLM scores.

Endpoints:
    POST /api/scoring/llm-rerank
        body: {top_n?: int = 30, force?: bool = False}
        → calls rerank_top_n(top_n, force). Returns the batch summary.

    POST /api/scoring/llm-rerank/{job_id}
        Ad-hoc single-job rescore. Always overwrites the existing row.
        Returns the persisted llm_job_score.

    GET /api/scoring/llm-scores?limit=50
        Sorted by semantic_score DESC, joined with job_posting so the
        UI can render a single ranked list with job context.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..db import get_conn, row_to_dict
from ..matching.llm_rerank import rerank_one, rerank_top_n
from ..security.rate_limit import rate_limit

log = logging.getLogger("jhh.scoring.llm_rerank")

router = APIRouter(prefix="/api/scoring", tags=["scoring"])


class LLMRerankBatchRequest(BaseModel):
    top_n: int = Field(default=30, ge=1, le=100)
    force: bool = Field(default=False)


@router.post("/llm-rerank")
@rate_limit("10/minute")
def post_llm_rerank_batch(
    request: Request = None,  # type: ignore[assignment]
    body: LLMRerankBatchRequest | None = None,
) -> dict:
    """Batch second-pass score the top-N deterministic-scored jobs.

    Skips jobs that already have an llm_job_score row unless force=True.
    """
    if body is None:
        body = LLMRerankBatchRequest()
    summary = rerank_top_n(top_n=int(body.top_n), force=bool(body.force))
    ok = not summary.get("skipped_no_provider") and summary.get("errors", 0) == 0
    return {"ok": ok, "data": summary}


@router.post("/llm-rerank/{job_id}")
@rate_limit("30/minute")
def post_llm_rerank_one(job_id: int, request: Request = None) -> dict:  # type: ignore[assignment]
    """Ad-hoc single-job semantic rescore. Always overwrites."""
    if int(job_id) <= 0:
        raise HTTPException(400, "job_id must be positive")
    result = rerank_one(int(job_id))
    if not result.get("ok"):
        # Surface 404 for the "job not found" case so the UI distinguishes
        # it from transient LLM failures.
        err = (result.get("error") or "").lower()
        if "not found" in err:
            raise HTTPException(404, result.get("error") or "job not found")
        if result.get("skipped_no_provider"):
            # Return 200 with ok=false so the UI can show a clean
            # "configure LLM first" toast rather than treating it as a bug.
            return {"ok": False, "data": result}
        return {"ok": False, "data": result, "error": result.get("error")}
    return {"ok": True, "data": result.get("data"),
            "evidence_pack_chars": result.get("evidence_pack_chars"),
            "elapsed_ms": result.get("elapsed_ms")}


@router.get("/llm-scores")
def get_llm_scores(limit: int = 50) -> dict:
    """Top jobs by semantic_score DESC, joined with their job_posting row."""
    limit = max(1, min(int(limit), 500))
    rows = get_conn().execute(
        """SELECT s.job_id, s.semantic_score, s.fit_summary,
                  s.strengths_json, s.gaps_json, s.red_flags_json,
                  s.recommended_action, s.llm_run_id, s.created_at,
                  j.title, j.company, j.location, j.source, j.apply_url,
                  m.overall_score AS deterministic_score
             FROM llm_job_score s
             JOIN job_posting j ON j.id = s.job_id
             LEFT JOIN job_match m ON m.job_id = s.job_id
             WHERE j.status NOT IN ('archived')
             ORDER BY s.semantic_score DESC NULLS LAST, s.created_at DESC
             LIMIT ?""",
        (limit,),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        for k in ("strengths_json", "gaps_json", "red_flags_json"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                try:
                    d[k.replace("_json", "")] = json.loads(v)
                except Exception:
                    d[k.replace("_json", "")] = []
            else:
                d[k.replace("_json", "")] = []
            d.pop(k, None)
        out.append(d)
    return {"ok": True, "data": out, "count": len(out)}

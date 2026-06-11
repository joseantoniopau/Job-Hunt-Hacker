"""Recruiter messages + interview prep endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..models.schemas import RecruiterMessageRequest
from ..tailoring import interview_prep, recruiter_messages

log = logging.getLogger("jhh.routers.recruiter")

router = APIRouter(prefix="/api", tags=["recruiter"])


class InterviewPrepRequest(BaseModel):
    job_id: int


@router.post("/recruiter/message")
def recruiter_message(body: RecruiterMessageRequest) -> dict:
    try:
        result = recruiter_messages.generate(job_id=body.job_id, channel=body.channel)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("recruiter message failed: %s", e)
        raise HTTPException(500, "recruiter message failed (see server log)")
    return {"ok": True, "data": result}


@router.post("/interview/prep")
def interview_prep_route(body: InterviewPrepRequest) -> dict:
    try:
        result = interview_prep.generate(job_id=body.job_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("interview prep failed: %s", e)
        raise HTTPException(500, "interview prep failed (see server log)")
    return {"ok": True, "data": result}

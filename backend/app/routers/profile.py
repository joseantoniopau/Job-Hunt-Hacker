"""GET/PUT /api/profile — singleton user profile."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException

from ..db import get_conn, row_to_dict, audit
from ..models.schemas import UserProfileIn, OK

router = APIRouter(prefix="/api", tags=["profile"])


_LIST_FIELDS = ["target_titles", "target_keywords", "excluded_keywords",
                "preferred_locations", "employment_types", "seniority_targets",
                "industries", "excluded_industries", "preferred_companies",
                "excluded_companies", "visa_preferences"]

_JSON_FIELDS = ["interview_availability_json", "scoring_weights_json"]


@router.get("/profile")
def get_profile() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if row is None:
        raise HTTPException(404, "profile row missing")
    return {"ok": True, "data": row_to_dict(row)}


@router.put("/profile")
def put_profile(body: UserProfileIn) -> OK:
    conn = get_conn()
    cols = []
    vals = []
    payload = body.model_dump(exclude_none=False)
    for k, v in payload.items():
        if k in _LIST_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v or []))
        elif k in _JSON_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v or {}))
        else:
            cols.append(f"{k} = ?")
            vals.append(v)
    cols.append("updated_at = ?")
    vals.append(time.time())
    sql = f"UPDATE user_profile SET {', '.join(cols)} WHERE id = 1"
    conn.execute(sql, vals)
    audit("profile_update", "user_profile", 1)
    return OK(detail="profile updated")

"""Assisted apply: builds the packet and marks an application as prepared.

Does NOT submit anything. User reviews the packet and submits via the
company's site themselves.
"""
from __future__ import annotations

import logging

from ..db import audit
from . import packet_builder, pipeline

log = logging.getLogger("jhh.assisted_apply")


def prepare(job_id: int, options: dict | None = None) -> dict:
    options = dict(options or {})
    options.setdefault("mode", "assisted")
    result = packet_builder.build(int(job_id), options)
    if not result.get("ok"):
        return result

    app_id = pipeline.create_application(
        job_id=int(job_id),
        status="prepared",
        mode="assisted",
        notes="Packet prepared for assisted application — review before submitting.",
    )
    try:
        audit(
            "assisted_apply_prepared",
            "application",
            int(app_id),
            job_id=int(job_id),
            packet_dir=result.get("packet_dir"),
        )
    except Exception:
        pass

    return {
        "ok": True,
        "application_id": app_id,
        "packet_dir": result.get("packet_dir"),
        "files": result.get("files"),
        "summary": result.get("summary"),
    }

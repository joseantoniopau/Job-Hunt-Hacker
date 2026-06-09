"""LLM observability — wraps any LLMProvider so every call is recorded.

Every `complete()` call:
  * gets a row in `llm_run` (start, finish, elapsed, status, error)
  * records the system + user prompt and output (truncated to keep the table
    small; raw bytes available at the time of the call only).
  * lets the caller pass a `stage` (e.g. "profile_inference",
    "llm_rerank", "interview_prep", "offer_analysis") so the UI can group
    activity by what the LLM is doing.

Other modules call `observed_complete(provider, stage, system, user, ...)`
instead of `provider.complete(...)` directly. The thin wrapper is
intentional — leaving raw `complete()` available means trivial fallbacks
(template provider, smoke tests) don't pay the logging cost.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from ..config import settings
from ..db import get_conn
from .base import LLMProvider

log = logging.getLogger("jhh.llm.obs")

# Keep persisted prompt/output snippets bounded so the table stays cheap to
# query. Full bodies remain in memory for the duration of the call.
_MAX_PERSIST_CHARS = 6000

# Context variable that the in-progress run can stash for tools that want
# to attach an llm_run_id (e.g. interview practice turns linking back to
# the LLM call that generated their feedback).
_current_run_id: ContextVar[Optional[int]] = ContextVar("_jhh_current_llm_run_id", default=None)


@dataclass
class RunHandle:
    run_id: int
    stage: str
    started_ts: float


def _truncate(s: str | None) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= _MAX_PERSIST_CHARS:
        return s
    return s[: _MAX_PERSIST_CHARS - 32] + f"\n…[truncated {len(s) - _MAX_PERSIST_CHARS + 32} chars]"


def _begin_run(stage: str, provider_name: str, model: str,
               system_text: str, user_text: str,
               target_type: str = "", target_id: int | None = None) -> RunHandle:
    """Insert a 'running' row before the call so the UI sees it live."""
    now = time.time()
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO llm_run
                (ts, finished_ts, provider, model, stage, target_type, target_id,
                 system_text, user_text, output_text, status, error,
                 prompt_chars, output_chars, elapsed_ms)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, 'running', NULL, ?, NULL, NULL)""",
                (now, provider_name, model, stage, target_type or "", target_id,
                 _truncate(system_text), _truncate(user_text),
                 len(system_text or "") + len(user_text or "")),
            )
            run_id = int(cur.lastrowid)
    except sqlite3.Error as exc:
        log.warning("could not record llm_run start (%s)", exc)
        run_id = -1
    return RunHandle(run_id=run_id, stage=stage, started_ts=now)


def _finish_run(handle: RunHandle, output_text: str, status: str, error: str = "") -> None:
    if handle.run_id <= 0:
        return
    now = time.time()
    elapsed_ms = int((now - handle.started_ts) * 1000)
    try:
        with get_conn() as conn:
            conn.execute(
                """UPDATE llm_run
                   SET finished_ts = ?, output_text = ?, status = ?, error = ?,
                       output_chars = ?, elapsed_ms = ?
                   WHERE id = ?""",
                (now, _truncate(output_text), status, error or "",
                 len(output_text or ""), elapsed_ms, handle.run_id),
            )
    except sqlite3.Error as exc:
        log.warning("could not record llm_run finish (%s)", exc)


def current_run_id() -> int | None:
    """Return the llm_run id of the call currently in progress on this
    context, if any. Lets downstream code link generated artifacts back to
    the LLM call that produced them."""
    return _current_run_id.get()


def observed_complete(
    provider: LLMProvider,
    stage: str,
    system: str,
    user: str,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.3,
    target_type: str = "",
    target_id: int | None = None,
) -> tuple[str, int]:
    """Call provider.complete(...) under observability.

    Returns (output_text, llm_run_id). When the run row could not be
    inserted, llm_run_id is -1 but the call still proceeds.
    """
    provider_name = getattr(provider, "name", type(provider).__name__)
    model = settings.llm_model or ""

    handle = _begin_run(stage, provider_name, model, system, user,
                        target_type=target_type, target_id=target_id)
    token = _current_run_id.set(handle.run_id if handle.run_id > 0 else None)
    try:
        try:
            output = provider.complete(system, user,
                                       max_tokens=max_tokens,
                                       temperature=temperature)
        except Exception as exc:  # noqa: BLE001
            _finish_run(handle, output_text="", status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        if not output:
            _finish_run(handle, output_text="", status="empty",
                        error="provider returned empty output")
            return "", handle.run_id
        _finish_run(handle, output_text=output, status="ok")
        return output, handle.run_id
    finally:
        _current_run_id.reset(token)


# ---- Read API used by /api/llm/runs ----

def list_runs(limit: int = 50, since_id: int = 0,
              stage: str | None = None) -> list[dict]:
    sql = """SELECT id, ts, finished_ts, provider, model, stage,
                    target_type, target_id, status, error,
                    prompt_chars, output_chars, elapsed_ms
             FROM llm_run
             WHERE id > ?"""
    params: list = [int(since_id)]
    if stage:
        sql += " AND stage = ?"
        params.append(stage)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = get_conn().execute(sql, params).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "ts": r["ts"],
            "finished_ts": r["finished_ts"],
            "provider": r["provider"],
            "model": r["model"],
            "stage": r["stage"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "status": r["status"],
            "error": r["error"],
            "prompt_chars": r["prompt_chars"],
            "output_chars": r["output_chars"],
            "elapsed_ms": r["elapsed_ms"],
        })
    return out


def get_run(run_id: int) -> dict | None:
    row = get_conn().execute(
        """SELECT id, ts, finished_ts, provider, model, stage,
                  target_type, target_id, status, error,
                  prompt_chars, output_chars, elapsed_ms,
                  system_text, user_text, output_text
           FROM llm_run WHERE id = ?""",
        (int(run_id),),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "ts": row["ts"],
        "finished_ts": row["finished_ts"],
        "provider": row["provider"],
        "model": row["model"],
        "stage": row["stage"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "status": row["status"],
        "error": row["error"],
        "prompt_chars": row["prompt_chars"],
        "output_chars": row["output_chars"],
        "elapsed_ms": row["elapsed_ms"],
        "system_text": row["system_text"],
        "user_text": row["user_text"],
        "output_text": row["output_text"],
    }


def active_count() -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM llm_run WHERE status = 'running'"
    ).fetchone()
    return int(row["n"] if row else 0)

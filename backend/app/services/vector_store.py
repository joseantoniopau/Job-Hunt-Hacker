"""Local vector store using SQLite + numpy cosine.

Embedding provider falls back through this chain:
  1. OpenAI text-embedding-3-small (if OPENAI_API_KEY)
  2. Local sentence-transformers (if installed)
  3. Deterministic hashing TF-IDF (always available)

The hashing fallback is dumb but real — it lets the app run with no deps.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
import time
from typing import Sequence

from ..config import settings
from ..db import get_conn

_HASH_DIM = 384


def _hash_embed(text: str, dim: int = _HASH_DIM) -> list[float]:
    text = (text or "").lower()
    vec = [0.0] * dim
    tokens = [t for t in text.replace("\n", " ").split(" ") if t]
    if not tokens:
        return vec
    # term frequencies bucketed by hash
    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        bucket = h % dim
        sign = 1.0 if (h >> 32) & 1 else -1.0
        vec[bucket] += sign
    # l2 normalize
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _try_openai_embed(text: str) -> list[float] | None:
    if not settings.openai_api_key:
        return None
    try:
        import httpx
        model = settings.embed_model or "text-embedding-3-small"
        base = settings.openai_base_url or "https://api.openai.com/v1"
        r = httpx.post(
            f"{base.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {settings.openai_api_key}",
                     "Content-Type": "application/json"},
            json={"input": text[:8000], "model": model},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception:
        return None


def _try_local_embed(text: str) -> list[float] | None:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        return None
    try:
        global _LOCAL_MODEL
        if "_LOCAL_MODEL" not in globals() or _LOCAL_MODEL is None:
            _LOCAL_MODEL = SentenceTransformer(settings.embed_model or "all-MiniLM-L6-v2")  # type: ignore
        v = _LOCAL_MODEL.encode([text[:8000]])[0]  # type: ignore
        return [float(x) for x in v]
    except Exception:
        return None


def embed(text: str) -> tuple[list[float], str]:
    provider = (settings.embed_provider or "auto").lower()
    if provider in ("openai", "auto"):
        v = _try_openai_embed(text)
        if v is not None:
            return v, settings.embed_model or "text-embedding-3-small"
    if provider in ("local", "auto"):
        v = _try_local_embed(text)
        if v is not None:
            return v, "local-st"
    return _hash_embed(text), "hash-md5-384"


def _pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


def add(owner_type: str, owner_id: int, text: str) -> int:
    vec, model = embed(text)
    blob = _pack(vec)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO embedding (owner_type, owner_id, text, vector, dim, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (owner_type, owner_id, text[:4000], blob, len(vec), model, time.time()),
    )
    return cur.lastrowid


def search(text: str, owner_type: str | None = None, top: int = 10) -> list[dict]:
    qvec, _ = embed(text)
    conn = get_conn()
    if owner_type:
        rows = conn.execute("SELECT * FROM embedding WHERE owner_type = ?", (owner_type,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM embedding").fetchall()
    scored: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        try:
            v = _unpack(r["vector"], r["dim"])
        except Exception:
            continue
        if len(v) != len(qvec):
            continue
        s = sum(a * b for a, b in zip(qvec, v))
        scored.append((s, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for s, r in scored[:top]:
        out.append({"id": r["id"], "owner_type": r["owner_type"], "owner_id": r["owner_id"],
                    "text": r["text"], "score": round(float(s), 4), "model": r["model"]})
    return out


def remove(owner_type: str, owner_id: int) -> int:
    conn = get_conn()
    cur = conn.execute("DELETE FROM embedding WHERE owner_type = ? AND owner_id = ?",
                       (owner_type, owner_id))
    return cur.rowcount

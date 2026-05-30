#!/usr/bin/env bash
# run.sh — launch the Job Hunt Hacker server.

set -e
ROOT="/Users/japa/Desktop/Job-Hunt-Hacker"
cd "$ROOT"

HOST="${JHH_HOST:-127.0.0.1}"
PORT="${JHH_PORT:-8731}"

PY="$(command -v python3 || command -v python)"

echo "Job Hunt Hacker → http://${HOST}:${PORT}"
exec "$PY" -m uvicorn backend.app.main:app --host "$HOST" --port "$PORT" --reload

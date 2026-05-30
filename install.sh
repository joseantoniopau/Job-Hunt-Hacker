#!/usr/bin/env bash
# install.sh — install the job-hunt-hacker skill end-to-end.
#
# Usage:  ./install.sh                      # full install
#         ./install.sh --skip-deps          # skip pip installs
#         ./install.sh --uninstall          # remove symlink

set -e

ROOT="/Users/japa/Desktop/Job-Hunt-Hacker"
SKILLS_DIR="$HOME/.claude/skills"
SKILL_LINK="$SKILLS_DIR/job-hunt-hacker"
PYTHON_BIN="$(command -v python3 || command -v python)"

SKIP_DEPS=0
UNINSTALL=0
for arg in "$@"; do
  case $arg in
    --skip-deps) SKIP_DEPS=1 ;;
    --uninstall) UNINSTALL=1 ;;
  esac
done

echo "=================================================================="
echo " JOB-HUNT-HACKER · install"
echo "=================================================================="
echo "root        : $ROOT"
echo "skills dir  : $SKILLS_DIR"
echo "python      : $PYTHON_BIN"
echo ""

if [ "$UNINSTALL" = "1" ]; then
  echo "[uninstall] removing symlink"
  [ -L "$SKILL_LINK" ] && rm "$SKILL_LINK" && echo "  symlink removed"
  echo "Done."
  exit 0
fi

# ----- 1. python deps -----
if [ "$SKIP_DEPS" = "0" ]; then
  echo "[1/6] installing python deps (may take a minute)"
  PIP_ARGS=""
  if [[ "$OSTYPE" == "darwin"* ]]; then
    PIP_ARGS="--break-system-packages"
  fi
  $PYTHON_BIN -m pip install --quiet --upgrade $PIP_ARGS \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    python-multipart \
    python-jobspy \
    pdfminer.six \
    python-docx \
    beautifulsoup4 \
    lxml \
    httpx \
    feedparser \
    apscheduler \
    python-dotenv \
    || echo "  WARN: pip install had issues; some adapters will be disabled gracefully"
  echo "  ok"
else
  echo "[1/6] dep install skipped"
fi

# ----- 2. ensure dirs -----
echo "[2/6] ensuring directories"
mkdir -p "$ROOT/cache" "$ROOT/uploads" "$ROOT/resumes" "$ROOT/packets" "$SKILLS_DIR"
echo "  ok"

# ----- 3. .env -----
echo "[3/6] checking .env"
if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "  created .env from example — edit it to add optional API keys"
else
  echo "  .env present"
fi

# ----- 4. seed data -----
echo "[4/6] refreshing seed data (remoteintech companies)"
$PYTHON_BIN "$ROOT/scripts/refresh_remoteintech.py" --quiet || \
  echo "  WARN: remoteintech refresh failed (non-fatal — adapter still works on cached data)"

# ----- 5. db init -----
echo "[5/6] initializing SQLite vault"
$PYTHON_BIN -c "import sys; sys.path.insert(0, '$ROOT'); from backend.app.db import init_db; init_db()" && \
  echo "  ok" || echo "  WARN: db init failed — check python deps"

# ----- 6. symlink to ~/.claude/skills/ -----
echo "[6/6] linking skill into ~/.claude/skills/"
if [ -L "$SKILL_LINK" ]; then
  rm "$SKILL_LINK"
fi
if [ -e "$SKILL_LINK" ]; then
  echo "  ERROR: $SKILL_LINK exists and is not a symlink. Move it aside and re-run."
  exit 1
fi
ln -s "$ROOT" "$SKILL_LINK"
echo "  symlinked $SKILL_LINK -> $ROOT"

echo ""
echo "=================================================================="
echo " DONE."
echo "=================================================================="
echo ""
echo " Next steps:"
echo "   1. (Optional) add API keys:       ./setup-keys.sh"
echo "   2. Launch the UI:                 ./run.sh"
echo "                                     → open http://127.0.0.1:8731"
echo "   3. Smoke-test everything:         python3 scripts/smoke_test.py"
echo ""
echo " The skill is now usable in Claude Code — invoke job-hunt-hacker"
echo " by mentioning jobs, resumes, applications, or career search."
echo ""

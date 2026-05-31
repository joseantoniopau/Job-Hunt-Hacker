#!/usr/bin/env bash
# Build a standalone `jhh` binary using PyInstaller.
#
# Output:  dist/jhh
#
# Usage:
#   scripts/build_binary.sh
#
# This script must be invoked from the repo root, but it `cd`s there
# defensively so calling it from anywhere works.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SPEC_FILE="scripts/build_binary.spec"
OUT_NAME="jhh"
DIST_DIR="dist"
BUILD_DIR="build"

# --- prerequisite: PyInstaller ---
if ! command -v pyinstaller >/dev/null 2>&1; then
    cat >&2 <<'EOF'
error: pyinstaller is not installed.

Install it inside your project's virtualenv:

    pip install pyinstaller

Then re-run:

    scripts/build_binary.sh

(PyInstaller is intentionally NOT in requirements.txt — it is only needed
when producing a release binary, not for normal development.)
EOF
    exit 127
fi

# --- prerequisite: launcher entry point ---
if [[ ! -f "scripts/launcher.py" ]]; then
    echo "error: scripts/launcher.py is missing — needed as the PyInstaller entry point" >&2
    exit 1
fi

if [[ ! -f "${SPEC_FILE}" ]]; then
    echo "error: spec file not found at ${SPEC_FILE}" >&2
    exit 1
fi

echo ">> building ${OUT_NAME} via pyinstaller (spec=${SPEC_FILE})"

# Wipe stale outputs so a previous failed build doesn't poison the result.
rm -rf "${BUILD_DIR}" "${DIST_DIR}/${OUT_NAME}" "${DIST_DIR}/${OUT_NAME}.exe" 2>/dev/null || true

pyinstaller \
    --clean \
    --noconfirm \
    --distpath "${DIST_DIR}" \
    --workpath "${BUILD_DIR}" \
    "${SPEC_FILE}"

# --- sanity check + summary ---
OUT_PATH=""
if [[ -f "${DIST_DIR}/${OUT_NAME}" ]]; then
    OUT_PATH="${DIST_DIR}/${OUT_NAME}"
elif [[ -f "${DIST_DIR}/${OUT_NAME}.exe" ]]; then
    OUT_PATH="${DIST_DIR}/${OUT_NAME}.exe"
fi

if [[ -z "${OUT_PATH}" || ! -f "${OUT_PATH}" ]]; then
    echo "error: build completed but expected output at ${DIST_DIR}/${OUT_NAME} was not produced" >&2
    exit 1
fi

chmod +x "${OUT_PATH}" 2>/dev/null || true
SIZE_HUMAN=$(du -h "${OUT_PATH}" | awk '{print $1}')
echo ">> built ${OUT_PATH} (${SIZE_HUMAN})"
echo ">> try it:  ${OUT_PATH}"

# PyInstaller spec for Job Hunt Hacker.
# Builds a single-file binary named `jhh` that wraps uvicorn + the FastAPI app.
#
# Build with:
#   pyinstaller --clean --noconfirm scripts/build_binary.spec
#
# The companion wrapper at scripts/build_binary.sh handles the call + copy.

# pylint: disable=undefined-variable
# (PyInstaller injects `Analysis`, `PYZ`, `EXE` at runtime.)

from pathlib import Path
import sys

# `__file__` is unavailable inside the spec context; rely on cwd which the
# wrapper sets to the repo root before invoking pyinstaller.
REPO_ROOT = Path.cwd().resolve()

ENTRY = str(REPO_ROOT / "scripts" / "launcher.py")

# Data dirs that the app reads from disk at runtime.
# Format: (source_path, destination_inside_bundle)
datas = [
    (str(REPO_ROOT / "ui"), "ui"),
    (str(REPO_ROOT / "docs"), "docs"),
    (str(REPO_ROOT / "data" / "seed"), "data/seed"),
]

# Routers are imported via __import__(f"backend.app.routers.{name}") so
# PyInstaller's static analyser misses them. List them explicitly.
hiddenimports = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "backend.app.main",
    "backend.app.config",
    "backend.app.db",
    "backend.app.routers.autopilot",
    "backend.app.routers.profile",
    "backend.app.routers.evidence",
    "backend.app.routers.vault",
    "backend.app.routers.search",
    "backend.app.routers.jobs",
    "backend.app.routers.resume",
    "backend.app.routers.cover_letter",
    "backend.app.routers.recruiter",
    "backend.app.routers.applications",
    "backend.app.routers.email",
    "backend.app.routers.calendar",
    "backend.app.routers.settings",
    "backend.app.routers.github",
    "backend.app.routers.urls",
    "backend.app.routers.scheduler",
    "backend.app.routers.auto_apply",
    "backend.app.routers.stats",
    "backend.app.routers.data",
    "backend.app.routers.updates",
]

# Heavy optional packages we never use — leaving them in bloats the binary.
excludes = [
    "tkinter",
    "matplotlib",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "notebook",
    "IPython",
    "jupyter",
    "pytest",
    "test",
    "tests",
]

block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="jhh",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

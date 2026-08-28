# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the xPST engine sidecar (Tauri 2 shell).

Builds a *one-file* macOS executable named ``xpst-engine`` that runs the
FastAPI dashboard entrypoint (``scripts/engine_entry.py``).  Tauri's
``bundle.externalBin`` requires a single executable file, so onedir is not an
option here — see the Tauri 2 externalBin docs.

Size strategy (target: shell .app total <= 120 MB):
    - Dashboard-only entrypoint: bypasses the CLI and the PySide6 desktop app.
    - PySide6/Qt excluded entirely (the shell's webview replaces it).
    - av/faster_whisper excluded (PyAV 18 ships dylibs under av/__dot__dylibs/
      which trips PyInstaller's bundle step — see PR #61 notes); both are
      lazy-imported only by offline transcription, which the engine never calls.
    - numpy/pandas/scipy/matplotlib/torch excluded: never imported by the
      dashboard code path.
    - googleapiclient static discovery docs stripped except youtube.v3 and
      youtubeAnalytics.v2 (same approach as build_macos.spec).

Build:
    pyinstaller build_engine.spec --noconfirm --distpath dist/engine
    cp dist/engine/xpst-engine \
       src-tauri/binaries/xpst-engine-$(rustc -vV -v 2>/dev/null | grep host | cut -d' ' -f2)
"""

from pathlib import Path

project_root = Path(SPECPATH)
src_dir = project_root / "src"
entry = project_root / "scripts" / "engine_entry.py"

# Modules excluded from the engine bundle. Everything here is either unused
# by the dashboard entrypoint or lazily imported behind try/except, so
# excluding drops nothing the engine needs at runtime.
EXCLUDED_MODULES = [
    # GUI stack the Tauri shell replaces.
    "PySide6",
    "shiboken6",
    "tkinter",
    # PyAV dylib bug (PR #61) + lazy transcription deps.
    "av",
    "faster_whisper",
    # Heavy numeric stacks never imported by the dashboard path.
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "torch",
    # Legacy/optional KB backends, all behind try/ImportError.
    "lancedb",
    "pyarrow",
    "onnxruntime",
    "fastembed",
    "tokenizers",
    "hf_xet",
    # Never runtime code.
    "mypy",
]

a = Analysis(
    [str(entry)],
    pathex=[str(src_dir)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "xpst.dashboard.server",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED_MODULES,
    noarchive=False,
)

# googleapiclient.discovery_cache ships static discovery docs for ~every
# Google API (~96MB). Keep only the docs the dashboard actually builds
# services for (youtube v3, youtubeAnalytics v2).
_KEEP_DOCS = (
    "discovery_cache/documents/youtube.v3.json",
    "discovery_cache/documents/youtubeAnalytics.v2.json",
)
a.datas = [
    entry_
    for entry_ in a.datas
    if "discovery_cache/documents/" not in entry_[0]
    or entry_[0].replace("\\", "/").endswith(_KEEP_DOCS)
]

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="xpst-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

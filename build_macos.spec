# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building xPST macOS .app bundle."""

import re
import subprocess
import sys
from pathlib import Path

block_cipher = None

project_root = Path(SPECPATH)
project_version = re.search(r'^version = "([^"]+)"', (project_root / "pyproject.toml").read_text(), re.MULTILINE).group(1)
source_commit = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=project_root, text=True,
).strip()
src_dir = project_root / "src"
qml_dir = src_dir / "xpst" / "desktop_app" / "qml"
assets_dir = project_root / "assets"
# Unified icon sourcing: prefer assets/ (shared with Windows), fall back to
# the legacy docs/ location for backward compatibility (W3-6).
mac_icon = assets_dir / "icon.icns"
if not mac_icon.exists():
    mac_icon = project_root / "docs" / "assets" / "xpst-icon.icns"

# Modules the bundle must NOT carry. Every entry is either unused by the app
# or lazily imported behind try/except, so excluding it drops nothing the
# desktop app needs at runtime:
# - tkinter/matplotlib/scipy/numpy/pandas: never imported by xpst.
# - av/faster_whisper: PyAV 18 ships dylibs under av/__dot__dylibs/ which
#   trips PyInstaller's BUNDLE copy step (FileNotFoundError on
#   libSvtAv1Enc). Both are lazy-imported only by offline transcription
#   (xpst.knowledge.ingest.transcribe), so excluding them drops nothing
#   the desktop app needs at runtime.
# - PySide6.QtWebEngine*: optional in-app YouTube playback preview only.
#   src/xpst/desktop_app/main.py imports QtWebEngineQuick inside try/except
#   and logs a warning when absent ("YouTube in-app playback disabled").
# - lancedb/pyarrow: legacy KB vector backend. sqlite-vec is the default
#   (xpst.knowledge.store.open_default_store) and every lancedb import is
#   wrapped in try/ImportError with a clean fall-through to sqlite-vec/JSON.
# - fastembed/onnxruntime/tokenizers/hf_xet: optional KB embeddings backend,
#   lazy-imported in xpst.knowledge.llm.embeddings behind try/ImportError.
# - mypy: type-checker that leaked into Frameworks; never runtime code.
# - PySide6.QtPdf/QtQuick3D/QtGraphs: PySide6 modules the app never imports.
EXCLUDED_MODULES = [
    "tkinter",
    "matplotlib",
    "scipy",
    "numpy",
    "pandas",
    "av",
    "faster_whisper",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngine",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "lancedb",
    "pyarrow",
    "onnxruntime",
    "fastembed",
    "tokenizers",
    "hf_xet",
    "mypy",
    "PySide6.QtPdf",
    "PySide6.QtQuick3D",
    "PySide6.QtGraphs",
]

a = Analysis(
    [str(src_dir / "xpst" / "desktop_app" / "main.py")],
    pathex=[str(src_dir)],
    binaries=[],
    datas=[
        (str(qml_dir), "xpst/desktop_app/qml"),
        (str(assets_dir), "assets") if assets_dir.exists() else (str(project_root / "build_macos.spec"), "assets"),
    ],
    hiddenimports=[
        "xpst",
        "xpst.config",
        "xpst.state",
        "xpst.engine",
        "xpst.cli",
        "xpst.diagnostics",
        "xpst.desktop_app.backend",
        "xpst.desktop_app.models",
        "xpst.providers",
        "xpst.readiness",
        "xpst.updater",
        "xpst.platforms.base",
        "xpst.platforms.youtube",
        "xpst.platforms.instagram",
        "xpst.platforms.x",
        "xpst.sources.base",
        "xpst.sources.local",
        "xpst.sources.tiktok",
        "xpst.sources.youtube",
        "xpst.sources.instagram",
        "xpst.sources.x",
        "xpst.utils.retry",
        "xpst.utils.circuit_breaker",
        "xpst.utils.quota",
        "xpst.utils.credentials",
        "xpst.analytics",
        "xpst.plugins",
        "PySide6.QtQuick",
        "PySide6.QtQuickControls2",
        "PySide6.QtQml",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED_MODULES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# googleapiclient.discovery_cache ships static discovery docs for ~every
# Google API (~96MB). The package itself must stay importable —
# googleapiclient.discovery.build(static_discovery=True) reads the doc via
# discovery_cache.get_static_doc() — so it is NOT in excludes. Instead, keep
# only the docs the app actually builds services for (grep src/ for
# "build(...)"): youtube v3 (platforms/youtube.py, connect.py,
# utils/sessions.py, analytics.py, dashboard/analytics.py) and
# youtubeAnalytics v2 (dashboard/analytics.py). All other APIs' docs are
# stripped from the collected data files.
_KEEP_DOCS = ("discovery_cache/documents/youtube.v3.json", "discovery_cache/documents/youtubeAnalytics.v2.json")
a.datas = [
    entry
    for entry in a.datas
    if "discovery_cache/documents/" not in entry[0]
    or entry[0].replace("\\", "/").endswith(_KEEP_DOCS)
]

# Qt ships its full framework/QML-plugin tree even for modules the app never
# uses (QtWebEngineCore.framework alone is ~216MB). The PySide6 module
# excludes above do not stop PyInstaller's Qt hooks from collecting those
# frameworks/QML plugins, so drop them explicitly here — from both binaries
# and datas — by name. The app's QML imports only: QtCore, QtGui, QtWidgets,
# QtQml, QtQuick (+Controls.Material / Layouts / Dialogs / styles) and
# QtMultimedia (ContentPage.qml / DetailPanel.qml). QtWebEngineView in
# PlaybackOverlay.qml is instantiated through a Loader and is optional
# (main.py logs a warning when QtWebEngine is absent). Keep-list neighbours
# (QtShaderTools, QtQuickDialogs2*, QtQuickControls2 styles, QtOpenGL,
# QtLabs*) are intentionally NOT dropped to avoid breaking QtQuick internals.
_QT_DROP_SUBSTRINGS = (
    "QtWebEngine",
    "QtPdf",
    "QtQuick3D",
    "QtGraphs",
    "Qt3D",
    "QtCharts",
    "QtDataVisualization",
    "QtLocation",
    "QtPositioning",
    "QtSensors",
    "QtWebSockets",
    "QtWebChannel",
    "QtTextToSpeech",
    "QtVirtualKeyboard",
    "QtTest",
    "QtScxml",
    "QtStateMachine",
    "QtRemoteObjects",
    "QtSpatialAudio",
    "QtWebView",
    "Qt5Compat",
    "QtSerialPort",
    "QtHelp",
    "QtUiTools",
    "QtSql",
    "QtNfc",
    "QtBluetooth",
    "QtGamepad",
    "QtNetworkAuth",
    "QtDesigner",
    "QtUiPlugin",
)


def _keep_entry(entry):
    name = entry[0].replace("\\", "/")
    return not any(pat in name for pat in _QT_DROP_SUBSTRINGS)


a.binaries = [entry for entry in a.binaries if _keep_entry(entry)]
a.datas = [entry for entry in a.datas if _keep_entry(entry)]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="xPST",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(mac_icon) if mac_icon.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="xPST",
)

app = BUNDLE(
    coll,
    name="xPST.app",
    icon=str(mac_icon) if mac_icon.exists() else None,
    bundle_identifier="com.tysais.xpst",
    info_plist={
        "CFBundleName": "xPST",
        "CFBundleDisplayName": "xPST - Cross-Posting Suite",
        "CFBundleVersion": project_version,
        "CFBundleShortVersionString": project_version,
        "XPSTSourceCommit": source_commit,
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)

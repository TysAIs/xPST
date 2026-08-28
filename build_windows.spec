# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building xPST Windows .exe."""

import sys
from pathlib import Path

block_cipher = None

project_root = Path(SPECPATH)
src_dir = project_root / "src"
qml_dir = src_dir / "xpst" / "desktop_app" / "qml"
assets_dir = project_root / "assets"

# Module excludes shared with build_macos.spec / build_linux.spec — see the
# rationale block in build_macos.spec for why each entry is safe to drop
# (unused by the app or lazily imported behind try/except).
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
        (str(assets_dir), "assets") if assets_dir.exists() else (str(project_root / "build_windows.spec"), "assets"),
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

# Keep only the static discovery docs the app builds services for
# (youtube v3, youtubeAnalytics v2); the discovery_cache package itself stays
# importable — see the matching comment in build_macos.spec.
_KEEP_DOCS = ("discovery_cache/documents/youtube.v3.json", "discovery_cache/documents/youtubeAnalytics.v2.json")
a.datas = [
    entry
    for entry in a.datas
    if "discovery_cache/documents/" not in entry[0]
    or entry[0].replace("\\", "/").endswith(_KEEP_DOCS)
]

# Qt ships its full framework/QML-plugin tree even for modules the app never
# uses. The PySide6 module excludes above do not stop PyInstaller's Qt hooks
# from collecting those frameworks/QML plugins, so drop them explicitly here
# — from both binaries and datas — by name. The app's QML imports only:
# QtCore, QtGui, QtWidgets, QtQml, QtQuick (+Controls.Material / Layouts /
# Dialogs / styles) and QtMultimedia (ContentPage.qml / DetailPanel.qml).
# QtWebEngineView in PlaybackOverlay.qml is instantiated through a Loader and
# is optional (main.py logs a warning when QtWebEngine is absent). Keep-list
# neighbours (QtShaderTools, QtQuickDialogs2*, QtQuickControls2 styles,
# QtOpenGL, QtLabs*) are intentionally NOT dropped to avoid breaking
# QtQuick internals.
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="xPST",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(assets_dir / "icon.ico") if (assets_dir / "icon.ico").exists() else None,
)

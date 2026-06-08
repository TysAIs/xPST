# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building xPST Windows .exe."""

import sys
from pathlib import Path

block_cipher = None

project_root = Path(SPECPATH)
src_dir = project_root / "src"
qml_dir = src_dir / "xpst" / "desktop_app" / "qml"
assets_dir = project_root / "assets"

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
        "xpst.desktop_app.backend",
        "xpst.desktop_app.models",
        "xpst.platforms.youtube",
        "xpst.platforms.instagram",
        "xpst.platforms.x",
        "xpst.sources.local",
        "xpst.utils.retry",
        "xpst.utils.circuit_breaker",
        "xpst.utils.quota",
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
    excludes=["tkinter", "matplotlib", "scipy", "numpy", "pandas"],
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

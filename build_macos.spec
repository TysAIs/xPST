# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building xPST macOS .app bundle."""

import sys
from pathlib import Path

block_cipher = None

project_root = Path(SPECPATH)
src_dir = project_root / "src"
qml_dir = src_dir / "xpst" / "desktop_app" / "qml"
assets_dir = project_root / "assets"
mac_icon = project_root / "docs" / "assets" / "xpst-icon.icns"

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
    bundle_identifier="com.xpst.app",
    info_plist={
        "CFBundleName": "xPST",
        "CFBundleDisplayName": "xPST - Cross-Posting Suite",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)

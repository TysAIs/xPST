# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building the xPST Linux binary (onefile).

Mirrors build_windows.spec: a single self-contained executable at dist/xPST.
Linux desktop bundles are distributed as a tarball of this binary plus
checksums/attestation produced by the release workflow (W3-2).
"""

import sys
from pathlib import Path

block_cipher = None

project_root = Path(SPECPATH)
src_dir = project_root / "src"
qml_dir = src_dir / "xpst" / "desktop_app" / "qml"
assets_dir = project_root / "assets"
# Unified icon sourcing under assets/ (shared with Windows/macOS, W3-6).
# Linux executables do not embed an icon the way Windows/macOS do; PyInstaller
# ignores a missing icon, so this is best-effort.
linux_icon = assets_dir / "icon.png"

a = Analysis(
    [str(src_dir / "xpst" / "desktop_app" / "main.py")],
    pathex=[str(src_dir)],
    binaries=[],
    datas=[
        (str(qml_dir), "xpst/desktop_app/qml"),
        (str(assets_dir), "assets") if assets_dir.exists() else (str(project_root / "build_linux.spec"), "assets"),
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
        "PySide6.QtMultimedia",
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
    icon=str(linux_icon) if linux_icon.exists() else None,
)

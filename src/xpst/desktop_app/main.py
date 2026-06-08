"""xPST Desktop App — entry point.

Creates QApplication, sets Material style, registers backend
controllers with QML engine, sets up system tray, and runs the event loop.
"""

import sys
import logging
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("xpst.desktop")

# ── PySide6 imports ──────────────────────────────────────────────────
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtCore import QUrl, QTimer
from PySide6.QtGui import QIcon

# Attempt Material style import (QtQuick.Controls)
try:
    from PySide6.QtQuickControls2 import QQuickStyle
except ImportError:
    # Older PySide6 versions may not expose this; set via env var fallback
    QQuickStyle = None  # type: ignore[assignment,misc]

# ── xPST desktop modules ────────────────────────────────────────────
from xpst.desktop_app.backend import AppController, ThemeProvider
from xpst.desktop_app.models import PostListModel


def _find_qml_path() -> Path:
    """Locate main.qml relative to this file."""
    here = Path(__file__).resolve().parent
    qml_path = here / "qml" / "main.qml"
    if qml_path.exists():
        return qml_path

    # Fallback: next to this file
    qml_path = here / "main.qml"
    if qml_path.exists():
        return qml_path

    # Last resort: return expected path (engine will report error)
    return here / "qml" / "main.qml"


def _setup_tray(app: QApplication, engine: QQmlApplicationEngine) -> QSystemTrayIcon | None:
    """Create a system tray icon with a basic context menu."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        logger.info("System tray not available, skipping tray icon")
        return None

    tray = QSystemTrayIcon(app)

    # Try to find an icon; fall back to default
    icon_paths = [
        Path(__file__).resolve().parent.parent / "assets" / "icons" / "icon-32x32.png",
        Path(__file__).resolve().parent / "assets" / "icon.png",
        Path(__file__).resolve().parent.parent.parent.parent / "assets" / "icon.png",
    ]
    icon_found = False
    for ip in icon_paths:
        if ip.exists():
            tray.setIcon(QIcon(str(ip)))
            icon_found = True
            break

    if not icon_found:
        # Use a stock icon as fallback
        tray.setIcon(QIcon.fromTheme("video-x-generic"))

    tray.setToolTip("xPST — Cross-Posting Tool")

    # Context menu
    menu = QMenu()

    show_action = menu.addAction("Show Window")
    refresh_action = menu.addAction("Refresh Data")
    menu.addSeparator()
    quit_action = menu.addAction("Quit")
    quit_action = menu.addAction("Quit")

    def _show_window() -> None:
        # Bring all QML windows to front
        root_objects = engine.rootObjects
        for obj in root_objects:
            if hasattr(obj, "show"):
                obj.show()
                obj.raise_()
                obj.requestActivate()

    def _refresh() -> None:
        root_objects = engine.rootObjects
        for obj in root_objects():
            if hasattr(obj, "refreshData"):
                obj.refreshData()

    show_action.triggered.connect(_show_window)
    refresh_action.triggered.connect(_refresh)
    quit_action.triggered.connect(app.quit)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: _show_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)

    tray.show()
    return tray


def main() -> int:
    """Launch the xPST desktop application."""
    # Must use QApplication (not QGuiApplication) for system tray support
    app = QApplication(sys.argv)
    app.setApplicationName("xPST")
    app.setOrganizationName("xPST")
    app.setApplicationDisplayName("xPST — Cross-Posting Tool")

    # Set Material style before creating the engine
    if QQuickStyle is not None:
        QQuickStyle.setStyle("Material")
    else:
        import os
        os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Material")

    # Create QML engine
    engine = QQmlApplicationEngine()

    # Add QML import path so the engine finds our module
    qml_dir = Path(__file__).parent / "qml"
    engine.addImportPath(str(qml_dir.parent))  # desktop_app/ so 'qml' module resolves

    # Create backend objects
    controller = AppController()
    post_model = PostListModel()
    post_model.load_from_state()

    # Connect controller refresh to model reload
    controller.dataChanged.connect(lambda: post_model.load_from_state())

    # Expose to QML
    engine.rootContext().setContextProperty("controller", controller)
    theme_provider = ThemeProvider()
    engine.rootContext().setContextProperty("theme", theme_provider)
    engine.rootContext().setContextProperty("postModel", post_model)

    # Load main.qml
    qml_path = _find_qml_path()
    logger.info("Loading QML from: %s", qml_path)

    if not qml_path.exists():
        logger.error("QML file not found: %s", qml_path)
        logger.error("Create %s or run the QML generation step first.", qml_path)
        # Don't crash — show a minimal window
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
        w = QWidget()
        w.setWindowTitle("xPST — QML Missing")
        layout = QVBoxLayout(w)
        label = QLabel(f"main.qml not found at:\n{qml_path}\n\nRun the QML generation step first.")
        layout.addWidget(label)
        w.resize(600, 200)
        w.show()
        return app.exec()

    engine.load(QUrl.fromLocalFile(str(qml_path)))

    # Check if QML loaded successfully
    if not engine.rootObjects:
        logger.error("Failed to load QML — check main.qml for errors")
        return 1

    # System tray (after engine is ready)
    tray = _setup_tray(app, engine)

    logger.info("xPST desktop app started")

    # Run event loop
    exit_code = app.exec()

    # Cleanup
    if tray:
        tray.hide()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

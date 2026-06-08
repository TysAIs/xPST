"""xPST Desktop App — entry point.

Creates QApplication, sets Material style, registers backend
controllers with QML engine, sets up system tray, and runs the event loop.
"""

import logging
import sys
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("xpst.desktop")

# ── PySide6 imports ──────────────────────────────────────────────────
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication, QMenu, QSplashScreen, QSystemTrayIcon

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


def _create_splash() -> QSplashScreen:
    """Create a splash screen with xPST branding."""
    # Try to load the real icon/logo first
    icon_paths = [
        Path(__file__).resolve().parent.parent.parent.parent / "assets" / "xpst-full.png",
        Path(__file__).resolve().parent.parent.parent.parent / "assets" / "icon.png",
    ]

    pixmap = None
    for ip in icon_paths:
        if ip.exists():
            pixmap = QPixmap(str(ip))
            break

    if pixmap is None or pixmap.isNull():
        # Generate a branded splash programmatically
        pixmap = QPixmap(400, 300)
        pixmap.fill(QColor("#0a0a0f"))
        painter = QPainter(pixmap)

        # Background panel
        painter.setBrush(QColor("#12121a"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(10, 10, 380, 280, 12, 12)

        # Lightning bolt
        font = QFont()
        font.setPixelSize(48)
        painter.setFont(font)
        painter.setPen(QColor("#6366f1"))
        painter.drawText(pixmap.rect().adjusted(0, -40, 0, 0), Qt.AlignCenter, "⚡")

        # App name
        font.setPixelSize(28)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#f0f0f5"))
        painter.drawText(pixmap.rect().adjusted(0, 30, 0, 0), Qt.AlignCenter, "xPST")

        # Subtitle
        font.setPixelSize(12)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor("#a0a0b0"))
        painter.drawText(pixmap.rect().adjusted(0, 70, 0, 0), Qt.AlignCenter, "Cross-Posting Suite")

        # Loading text
        font.setPixelSize(10)
        painter.setPen(QColor("#6b6b80"))
        painter.drawText(pixmap.rect().adjusted(0, 110, 0, 0), Qt.AlignCenter, "Loading...")

        painter.end()

    splash = QSplashScreen(pixmap)
    splash.setWindowFlags(Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    return splash


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
    post_now_action = menu.addAction("Post Now...")
    check_health_action = menu.addAction("Check Health")
    refresh_action = menu.addAction("Refresh Data")
    menu.addSeparator()
    quit_action = menu.addAction("Quit")

    def _show_window() -> None:
        # Bring all QML windows to front
        root_objects = engine.rootObjects()
        for obj in root_objects:
            if hasattr(obj, "show"):
                obj.show()
                obj.raise_()
                obj.requestActivate()

    def _toggle_window() -> None:
        root_objects = engine.rootObjects()
        for obj in root_objects:
            if hasattr(obj, "isVisible") and hasattr(obj, "show"):
                if obj.isVisible():
                    obj.hide()
                else:
                    obj.show()
                    obj.raise_()
                    obj.requestActivate()

    def _refresh() -> None:
        root_objects = engine.rootObjects()
        for obj in root_objects:
            if hasattr(obj, "refreshData"):
                obj.refreshData()

    def _post_now() -> None:
        from PySide6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            None, "Select video to post", "",
            "Video Files (*.mp4 *.mov *.avi *.mkv *.webm);;All Files (*)"
        )
        if file_path:
            controller_obj = engine.rootContext().contextProperty("controller")
            if controller_obj:
                caption = Path(file_path).stem
                controller_obj.postVideo(file_path, caption)
                tray.showMessage("xPST", f"Posting: {Path(file_path).name}", QSystemTrayIcon.Information, 3000)

    def _check_health() -> None:
        controller_obj = engine.rootContext().contextProperty("controller")
        if controller_obj:
            health_json = controller_obj.getHealth()
            try:
                import json
                health = json.loads(health_json)
                healthy_count = sum(
                    1 for p in health.values()
                    if p.get("status") in ("ok", "healthy", "connected")
                )
                total = len(health)
                tray.showMessage(
                    "xPST Health",
                    f"{healthy_count}/{total} platforms healthy",
                    QSystemTrayIcon.Information, 5000
                )
            except Exception:
                tray.showMessage("xPST Health", "Health check complete", QSystemTrayIcon.Information, 3000)

    show_action.triggered.connect(_show_window)
    post_now_action.triggered.connect(_post_now)
    check_health_action.triggered.connect(_check_health)
    refresh_action.triggered.connect(_refresh)
    quit_action.triggered.connect(app.quit)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: _toggle_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

    tray.show()
    return tray


def main(no_splash: bool = False) -> int:
    """Launch the xPST desktop application."""
    # Must use QApplication (not QGuiApplication) for system tray support
    app = QApplication(sys.argv)
    app.setApplicationName("xPST")
    app.setOrganizationName("xPST")
    app.setOrganizationDomain("xpst.app")
    app.setApplicationDisplayName("xPST — Cross-Posting Tool")

    # Set Material style before creating the engine
    if QQuickStyle is not None:
        QQuickStyle.setStyle("Material")
    else:
        import os
        os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Material")

    # ── Splash Screen ────────────────────────────────────────────────
    no_splash = "--no-splash" in sys.argv
    splash = None
    if not no_splash:
        splash = _create_splash()
        splash.show()
        app.processEvents()  # ensure splash is painted before heavy init
        splash.showMessage("Loading config...", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
        app.processEvents()

    # Create QML engine
    engine = QQmlApplicationEngine()
    if splash:
        splash.showMessage("Initializing state...", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
        app.processEvents()

    # Add QML import path so the engine finds our module
    qml_dir = Path(__file__).parent / "qml"
    engine.addImportPath(str(qml_dir.parent))  # desktop_app/ so 'qml' module resolves

    # Create backend objects
    controller = AppController()
    post_model = PostListModel()
    post_model.load_from_state()
    if splash:
        splash.showMessage("Loading plugins...", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
    app.processEvents()

    # Connect controller refresh to model reload
    controller.dataChanged.connect(lambda: post_model.load_from_state())

    # Expose to QML
    engine.rootContext().setContextProperty("controller", controller)
    theme_provider = ThemeProvider()
    engine.rootContext().setContextProperty("theme", theme_provider)
    engine.rootContext().setContextProperty("postModel", post_model)
    engine.rootContext().setContextProperty("noSplashMode", no_splash)
    if splash:
        splash.showMessage("Starting engine...", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
    app.processEvents()

    # Load main.qml
    qml_path = _find_qml_path()
    logger.info("Loading QML from: %s", qml_path)
    if splash:
        splash.showMessage("Building UI...", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
    app.processEvents()

    if not qml_path.exists():
        if splash:
            splash.close()
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
    if not engine.rootObjects():
        if splash:
            splash.close()
        logger.error("Failed to load QML — check main.qml for errors")
        return 1

    # Close splash once the window is visible
    if splash:
        QTimer.singleShot(800, splash.close)

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

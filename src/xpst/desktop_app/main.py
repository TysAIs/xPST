"""xPST Desktop App — entry point.

Creates QApplication, sets Material style, registers backend
controllers with QML engine, sets up system tray, and runs the event loop.
"""

import json
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
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication, QMenu, QSplashScreen, QSystemTrayIcon

from xpst.desktop_app.resource_path import (
    first_existing,
    resolve_app_icon,
    resource_path,
)
from xpst.desktop_app.splash_sizing import scaled_splash_size

# Attempt Material style import (QtQuick.Controls)
try:
    from PySide6.QtQuickControls2 import QQuickStyle
except ImportError:
    # Older PySide6 versions may not expose this; set via env var fallback
    QQuickStyle = None  # type: ignore[assignment,misc]

# ── xPST desktop modules ────────────────────────────────────────────
from xpst.desktop_app import icon_glyphs
from xpst.desktop_app.backend import (
    AppController,
    ThemeProvider,
    _default_ui_font,
)
from xpst.desktop_app.models import PostListModel


def _find_qml_path() -> Path:
    """Locate main.qml in the frozen bundle or the source tree."""
    # Frozen bundles ship QML under xpst/desktop_app/qml relative to _MEIPASS.
    frozen_qml = resource_path("xpst", "desktop_app", "qml", "main.qml")

    here = Path(__file__).resolve().parent
    found = first_existing(
        frozen_qml,
        here / "qml" / "main.qml",
        here / "main.qml",
    )
    if found is not None:
        return found

    # Last resort: return expected path (engine will report error). Warn so a
    # frozen-build QML-not-found is diagnosable rather than silent.
    logger.warning(
        "main.qml not found in frozen bundle (%s) or source tree; "
        "falling back to %s — QML load will likely fail.",
        frozen_qml,
        here / "qml" / "main.qml",
    )
    return here / "qml" / "main.qml"


def _create_splash() -> QSplashScreen:
    """Create a splash screen with xPST branding."""
    # Try to load the real brand image first (frozen-aware, unified under
    # assets/). The splash keeps its programmatic fallback below, so a missing
    # image is non-fatal here — only the tray/app icon hard-fails (W3-6).
    splash_image = first_existing(
        resource_path("assets", "xpst-full.png"),
        resource_path("assets", "icon.png"),
    )

    pixmap = QPixmap(str(splash_image)) if splash_image is not None else None

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

    # Bound the splash to a sane size so a large brand image (e.g. the
    # 1024x1024 app icon) never blits full-screen on launch.
    target_w, target_h = scaled_splash_size(pixmap.width(), pixmap.height())
    if (target_w, target_h) != (pixmap.width(), pixmap.height()):
        pixmap = pixmap.scaled(
            target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )

    splash = QSplashScreen(pixmap)
    splash.setWindowFlags(Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    return splash


def _setup_tray(app: QApplication, engine: QQmlApplicationEngine) -> QSystemTrayIcon | None:
    """Create a system tray icon with a basic context menu."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        logger.info("System tray not available, skipping tray icon")
        return None

    tray = QSystemTrayIcon(app)

    # Resolve the tray icon from the unified assets/ source (frozen-aware).
    tray_icon = resolve_app_icon(required=False)
    if tray_icon is not None:
        tray.setIcon(QIcon(str(tray_icon)))
    else:
        # No bundled icon: fall back to a stock theme icon and warn loudly so
        # the missing-asset condition is visible (W3-6).
        logger.warning(
            "No app icon found under assets/ (expected icon.png, xpst-full.png, "
            "or xpst-icon.png); falling back to a stock theme icon."
        )
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
                if hasattr(controller_obj, "previewPost"):
                    try:
                        preview = json.loads(controller_obj.previewPost(file_path, caption, ""))
                        if not preview.get("ready"):
                            blocking = preview.get("blocking") or [preview.get("error") or "Post is not ready"]
                            tray.showMessage("xPST", str(blocking[0]), QSystemTrayIcon.Warning, 5000)
                            return
                    except Exception as exc:
                        tray.showMessage("xPST", f"Post preview failed: {exc}", QSystemTrayIcon.Warning, 5000)
                        return
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


def _load_icon_font() -> bool:
    """Register the bundled Lucide icon font with Qt's font database (W4-5).

    Returns True if the font loaded and registered under the expected family.
    The icon glyphs exposed by ThemeProvider only render if this family is
    available, so a load failure is logged loudly. Non-fatal: the app still
    runs (icons just fall back to whatever the family resolves to).
    """
    font_path = icon_glyphs.icon_font_path()
    if not font_path.exists():
        logger.warning(
            "Icon font not found at %s; icon glyphs may render as boxes.",
            font_path,
        )
        return False
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id < 0:
        logger.warning("Failed to register icon font %s with Qt.", font_path)
        return False
    families = QFontDatabase.applicationFontFamilies(font_id)
    if icon_glyphs.ICON_FONT_FAMILY not in families:
        logger.warning(
            "Icon font registered as %s, expected %s; icon bindings may not "
            "resolve.",
            families,
            icon_glyphs.ICON_FONT_FAMILY,
        )
    return True


def _load_ui_font() -> bool:
    """Register the bundled Inter UI font before loading QML.

    Headless/offscreen Qt environments can expose no system sans fonts. If the
    Lucide icon font is the only registered TTF, normal labels fall back to it
    and render as missing glyphs.
    """
    font_path = icon_glyphs.ui_font_path()
    if not font_path.exists():
        logger.warning("UI font not found at %s; text may use platform fallback.", font_path)
        return False
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id < 0:
        logger.warning("Failed to register UI font %s with Qt.", font_path)
        return False
    families = QFontDatabase.applicationFontFamilies(font_id)
    if icon_glyphs.UI_FONT_FAMILY not in families:
        logger.warning(
            "UI font registered as %s, expected %s; text metrics may drift.",
            families,
            icon_glyphs.UI_FONT_FAMILY,
        )
    return True


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

    # Apply a platform-aware default UI font so text metrics don't drift on
    # macOS/Linux (W4-7). QML elements that don't set font.family inherit this.
    _load_ui_font()
    app.setFont(QFont(_default_ui_font()))

    # Register the bundled icon font so theme.icon* glyphs render (W4-5).
    _load_icon_font()

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

    # Add QML import path so the engine finds our 'qml' module. Frozen bundles
    # ship it under xpst/desktop_app/qml relative to _MEIPASS; in source it is
    # next to this file. We add the *parent* of the qml dir so 'qml' resolves.
    qml_main = _find_qml_path()
    engine.addImportPath(str(qml_main.parent.parent))
    # The versioned module URI 'xpst.desktop_app.qml 1.0' (declared in
    # qml/qmldir) resolves from the root CONTAINING the xpst/ tree:
    # sys._MEIPASS in a frozen bundle, src/ in a checkout. Without this the
    # frozen app fails with 'module "xpst.desktop_app.qml" is not installed'.
    engine.addImportPath(str(qml_main.parents[3]))

    # Create backend objects (lightweight - defer heavy init)
    controller = AppController()
    post_model = PostListModel()
    post_model.load_from_state()
    if splash:
        splash.showMessage("Loading plugins...", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
    app.processEvents()

    # Connect controller refresh to model reload
    controller.dataChanged.connect(lambda: post_model.load_from_state())
    app.aboutToQuit.connect(controller.stopMcpServer)

    # Expose to QML
    engine.rootContext().setContextProperty("controller", controller)
    theme_provider = ThemeProvider()
    engine.rootContext().setContextProperty("theme", theme_provider)
    engine.rootContext().setContextProperty("postModel", post_model)
    # Named xpstNoSplash because a root QML property of the same name would
    # shadow the context property and self-bind (G40).
    engine.rootContext().setContextProperty("xpstNoSplash", no_splash)
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

    # Close splash as soon as the window is up. Use finish() so the splash
    # hides exactly when the root window shows; keep a short fallback timer.
    if splash:
        root = engine.rootObjects()[0]
        try:
            splash.finish(root)
        except (TypeError, RuntimeError):
            pass
        QTimer.singleShot(120, splash.close)

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

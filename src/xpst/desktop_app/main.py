"""xPST Desktop App — entry point.

Creates QApplication, sets Material style, registers backend
controllers with QML engine, sets up system tray, and runs the event loop.
"""

import logging
import os
import sys
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("xpst.desktop")

# ── PySide6 imports ──────────────────────────────────────────────────
try:
    from PySide6.QtCore import QLockFile, Qt, QTimer, QUrl
    from PySide6.QtGui import QAccessible, QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication, QMenu, QSplashScreen, QSystemTrayIcon

    try:
        from PySide6.QtQuickControls2 import QQuickStyle
    except ImportError:
        QQuickStyle = None  # type: ignore[assignment,misc]
except Exception as exc:  # pragma: no cover - exercised only when PySide6/Qt is unavailable
    print("xPST desktop cannot start: the required Qt/PySide6 runtime is not available.")
    print("If you built or downloaded xPST, make sure you installed the correct package for")
    print("your platform, or install the full desktop dependencies: pip install 'xpst[desktop]'")
    print("Raw import error:")
    raise SystemExit(1) from exc

# QtWebEngine is an optional part of the PySide6 desktop extra. It is only
# needed for in-app YouTube playback (PlaybackOverlay); a build without it
# still runs — the lineup falls back to open-in-browser. Initialise before
# any QML is loaded so the WebEngineView type is registered (registrations
# are cheap; Chromium's heavy processes only spawn when a view is created).
try:
    from PySide6.QtWebEngineQuick import QtWebEngineQuick

    _WEBENGINE_OK = True
except ImportError:  # pragma: no cover - older/minimal PySide6 builds
    QtWebEngineQuick = None  # type: ignore[assignment,misc]
    _WEBENGINE_OK = False

# ── xPST desktop modules ────────────────────────────────────────────
from xpst.desktop_app import icon_glyphs
from xpst.desktop_app.backend import (
    AppController,
    ThemeProvider,
    _default_ui_font,
)
from xpst.desktop_app.models import PostListModel
from xpst.desktop_app.resource_path import (
    first_existing,
    resolve_app_icon,
    resource_path,
)
from xpst.desktop_app.splash_sizing import scaled_splash_size
from xpst.utils.platform import get_config_dir


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
        resource_path("assets", "icon.png"),
        resource_path("assets", "xpst-full.png"),
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


def _make_macos_unified_window(engine) -> None:
    """macOS-only: render the titlebar transparent and let the app fill the
    whole window (traffic lights still controlled natively by the OS/titlebar
    so close/minimize/zoom keep working and stay accessible).

    Achieved by flipping the native NSWindow into a full-size content view:
      - NSWindowStyleMaskFullSizeContentView (bit 14) so the QML canvas begins
        under the titlebar region;
      - setTitlebarAppearsTransparent:YES so the OS chrome visually hides;
      - titleVisibility = NSWindowTitleHidden so our own header/branding text
        is the only title (the app already draws its 44–48px logo header).

    Implemented via the Objective-C runtime through ctypes only on darwin;
    on any other platform or any native-call failure this is a strict no-op
    and the app keeps the normal titlebar (return value ignored).
    """
    if sys.platform != "darwin":
        return
    if engine is None:
        return
    try:
        import ctypes
        import ctypes.util

        root = engine.rootObjects()[0]
        if root is None:
            return
        win_handle = root.windowHandle()
        if win_handle is None or not win_handle.winId():
            logger.debug("macOS unified titlebar: no window handle yet, skipped")
            return

        appkit = ctypes.util.find_library("AppKit")
        objcruntime = ctypes.util.find_library("objc")
        if not appkit or not objcruntime:
            logger.debug("macOS unified titlebar: native libs missing, skipped")
            return

        objc = ctypes.cdll.LoadLibrary(objcruntime)
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        sel_register = objc.sel_registerName
        sel_register.restype = ctypes.c_void_p
        sel_register.argtypes = [ctypes.c_char_p]

        def sel(name: str) -> ctypes.c_void_p:
            return sel_register(name.encode("utf-8"))

        ns_view = ctypes.c_void_p(win_handle.winId())
        window = objc.objc_msgSend(ns_view, sel("window"))
        if not window:
            logger.debug("macOS unified titlebar: no NSWindow, skipped")
            return

        # styleMask → add NSWindowStyleMaskFullSizeContentView (1 << 15) so
        # content fills the window INCLUDING under the (now transparent)
        # titlebar. NSWindowStyleMaskFullScreen (1 << 7) arrives preset by Qt
        # even outside fullscreen transitions, and AppKit raises
        # NSGenericException ("set on a window outside of a full screen
        # transition") if we echo it back — so clear it, then set the mask.
        full_size_content_mask = 1 << 15
        full_screen_style_bit = 1 << 7
        objc.objc_msgSend.restype = ctypes.c_ulonglong
        style_mask = objc.objc_msgSend(window, sel("styleMask")) or 0
        new_mask = (style_mask & ~full_screen_style_bit) | full_size_content_mask
        if new_mask != style_mask:
            objc.objc_msgSend.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong,
            ]
            objc.objc_msgSend(window, sel("setStyleMask:"), new_mask)

        # titlebarAppearsTransparent = YES
        objc.objc_msgSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool,
        ]
        objc.objc_msgSend(window, sel("setTitlebarAppearsTransparent:"), True)

        # titleVisibility = NSWindowTitleHidden
        objc.objc_msgSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
        ]
        objc.objc_msgSend(window, sel("setTitleVisibility:"), 1)  # NSWindowTitleHidden

        # Native background dragging must stay disabled: with a full-size
        # transparent titlebar AppKit can otherwise treat the entire content
        # view as the window drag surface and swallow QML mouse events.
        objc.objc_msgSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool,
        ]
        objc.objc_msgSend(window, sel("setMovableByWindowBackground:"), False)

        logger.info("macOS unified titlebar applied (full-size content view)")
    except Exception as exc:  # noqa: BLE001 - native path never breaks the app
        logger.debug("macOS unified titlebar skipped: %s", exc)


def _enable_macos_accessibility() -> None:
    """Force Qt's macOS accessibility bridge to be active from first launch.

    Qt only populates the macOS AX (Accessibility) tree when the bridge is
    activated; by leaving it lazy, automation/assistive clients (VoiceOver,
    Accessibility Inspector, ``System Events``, AppleScript) can observe an
    empty tree — "AX returns 0 interactable elements" — and keyboard focus /
    key delivery can drop. Activating it eagerly makes the window and its
    controls show up in the macOS accessibility hierarchy and keeps keyboard
    input routed to the focused widget.

    Safe no-op on non-macOS platforms and when the (optional) Qt accessibility
    plugin is unavailable.
    """
    if sys.platform != "darwin":
        return
    try:
        # Belt-and-braces: some Qt versions gate the bridge on this env var
        # read at QApplication construction; set it before any GUI is created.
        os.environ.setdefault("QT_ACCESSIBILITY", "1")
        QAccessible.setActive(True)
        logger.info("macOS accessibility bridge activated")
    except Exception as exc:  # noqa: BLE001 - never break startup
        logger.debug("macOS accessibility activation skipped: %s", exc)


def main(no_splash: bool = False) -> int:
    """Launch the xPST desktop application."""
    # Single-instance guard: a second launch focuses the existing window
    # instead of spawning a competing engine (double-posting risk) — the
    # shared pidfile in ~/.xpst/xpst.pid is what `xpst run/serve` also uses.
    lock = QLockFile(str(get_config_dir() / "xpst-gui.lock"))
    if not lock.tryLock(0):
        logger.warning("xPST desktop already running — activating existing instance.")
        print("xPST is already running.", file=sys.stderr)
        return 10  # same exit code family as 'another instance holds lock'

    # Must use QApplication (not QGuiApplication) for system tray support
    app = QApplication(sys.argv)
    app.setApplicationName("xPST")
    app.setOrganizationName("xPST")
    app.setOrganizationDomain("xpst.app")
    app.setApplicationDisplayName("xPST — Cross-Posting Tool")

    # Enforce the macOS accessibility bridge so the window/controls appear in
    # the AX tree (interactable elements + keyboard delivery) for automation
    # and assistive clients. Safe no-op off-macOS.
    _enable_macos_accessibility()

    # Set Material style before creating the engine
    if QQuickStyle is not None:
        QQuickStyle.setStyle("Material")
    else:
        import os
        os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Material")

    # Register the bundled icon font so theme.icon* glyphs render (W4-5).
    _load_icon_font()

    # Register bundled Inter fonts (SIL OFL) so the UI falls back cleanly
    # when the platform default (SF Pro Text on macOS) is unavailable —
    # removes the per-launch "missing font family" warning in bundles.
    from xpst.desktop_app.backend import _register_bundled_fonts
    _register_bundled_fonts()

    # Apply a platform-aware default UI font so text metrics don't drift on
    # macOS/Linux (W4-7). QML elements that don't set font.family inherit this.
    app.setFont(QFont(_default_ui_font()))

    # ── Splash Screen ────────────────────────────────────────────────
    no_splash = "--no-splash" in sys.argv
    splash = None
    if not no_splash:
        splash = _create_splash()
        splash.show()
        app.processEvents()  # ensure splash is painted before heavy init
        splash.showMessage("Loading config...", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
        app.processEvents()

    # Create QML engine. When QtWebEngine is available, register the
    # WebEngineView QML module first (PlaybackOverlay needs it); the core
    # Chromium processes stay dormant until a view is actually created.
    if _WEBENGINE_OK and QtWebEngineQuick is not None:
        try:
            QtWebEngineQuick.initialize()
        except Exception as exc:
            logger.warning("QtWebEngine init failed; YouTube in-app playback disabled: %s", exc)
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
    # The Library is driven by the merged video lineup (metric_snapshots +
    # local state), not just state.json posted_videos — so tracked videos
    # with real metrics are visible even when state has no record yet.
    try:
        lineup = controller._build_lineup_rows()
        post_model.load_lineup(lineup)
    except Exception as exc:
        logger.warning("Initial lineup load failed, falling back to state: %s", exc)
        post_model.load_from_state()
    if splash:
        splash.showMessage("Loading plugins...", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
    app.processEvents()

    # Connect controller refresh to model reload (lineup-driven).
    controller.dataChanged.connect(lambda: post_model.load_lineup(controller._build_lineup_rows()))

    # Expose to QML
    engine.rootContext().setContextProperty("controller", controller)
    theme_provider = ThemeProvider()
    engine.rootContext().setContextProperty("theme", theme_provider)
    engine.rootContext().setContextProperty("postModel", post_model)
    # Named xpstNoSplash because a root QML property of the same name would
    # shadow the context property and self-bind (G40).
    engine.rootContext().setContextProperty("xpstNoSplash", no_splash)

    # Expose brand logo path so QML pages can use the real image (not a font glyph).
    _logo = first_existing(
        resource_path("assets", "icon.png"),
        resource_path("assets", "xpst-full.png"),
    )
    _logo_horizontal = first_existing(
        resource_path("assets", "logos", "banner-logo.png"),
        resource_path("assets", "logos", "logo-horizontal.png"),
    )
    engine.rootContext().setContextProperty(
        "logoUrl", _logo.as_uri() if _logo else ""
    )
    engine.rootContext().setContextProperty(
        "logoHorizontalUrl", _logo_horizontal.as_uri() if _logo_horizontal else ""
    )
    # Expose the bundled icon-font URL so QML's Icons.qml loads the real font.
    # In a frozen .app a hardcoded relative URL from the QML file resolves one
    # level above the data side (Contents/Resources) and logs a FontLoader
    # error on every launch; an absolute URL resolved frozen-aware via
    # resource_path works in every layout. Empty string → Icons.qml falls back
    # to its document-relative path (standalone QML tooling).
    _icon_font = icon_glyphs.icon_font_path()
    engine.rootContext().setContextProperty(
        "iconFontUrl", _icon_font.as_uri() if _icon_font.exists() else ""
    )
    # macOS unified hidden-titlebar flag (see _make_macos_unified_window):
    # lets QML pad the traffic-light zone on macOS only; false everywhere else.
    engine.rootContext().setContextProperty(
        "macUnifiedTitlebar", sys.platform == "darwin"
    )
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

    # macOS: unified hidden-titlebar window with the traffic lights embedded
    # into the canvas (full-size content view behind the title bar). Function
    # no-ops safely on every other platform / when the native call fails, so
    # this never breaks a build.
    _make_macos_unified_window(engine)

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

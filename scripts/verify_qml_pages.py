# ruff: noqa: N802,N815
"""Smoke-load the xPST desktop QML shell and every page.

This is intentionally credential-free. It injects the same ThemeProvider
used by the app plus a tiny controller stub so CI can catch QML syntax,
binding, and missing-theme-token errors on every platform.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine
from PySide6.QtWidgets import QApplication

from xpst.desktop_app.backend import ThemeProvider


class SmokeController(QObject):
    dataChanged = Signal()
    notification = Signal(str, bool)
    settingsSaved = Signal(bool, str)
    connectResult = Signal(str)

    @Property(int, notify=dataChanged)
    def totalPosts(self) -> int:
        return 0

    @Property(int, notify=dataChanged)
    def totalReach(self) -> int:
        return 0

    @Property(str, notify=dataChanged)
    def bestPlatform(self) -> str:
        return "-"

    @Property(int, notify=dataChanged)
    def postsThisWeek(self) -> int:
        return 0

    @Property(str, notify=dataChanged)
    def platformHealth(self) -> str:
        data = {
            "youtube": {"name": "youtube", "label": "YouTube", "status": "unknown"},
            "instagram": {"name": "instagram", "label": "Instagram", "status": "unknown"},
            "x": {"name": "x", "label": "X", "status": "unknown"},
            "tiktok": {"name": "tiktok", "label": "TikTok", "status": "unknown"},
        }
        return json.dumps(data)

    @Property(str, notify=dataChanged)
    def recentPosts(self) -> str:
        return "[]"

    @Property(str, notify=dataChanged)
    def analyticsData(self) -> str:
        return json.dumps({"available": False})

    @Property(str, notify=dataChanged)
    def configData(self) -> str:
        return json.dumps(
            {
                "download_dir": "",
                "default_caption": "",
                "youtube_enabled": True,
                "instagram_enabled": True,
                "x_enabled": True,
                "tiktok_enabled": True,
                "rate_limit_posts": 10,
                "rate_limit_minutes": 60,
                "notifications_enabled": True,
                "dark_mode": False,
            }
        )

    @Property(str, notify=dataChanged)
    def quotaData(self) -> str:
        return "{}"

    @Slot()
    def refreshData(self) -> None:
        self.dataChanged.emit()

    @Slot(str)
    def connectPlatform(self, platform: str) -> None:
        self.connectResult.emit(json.dumps({"ok": False, "platform": platform}))

    @Slot(str)
    def connectPlatformAsync(self, platform: str) -> None:
        self.connectPlatform(platform)

    @Slot(str, str)
    def postVideo(self, _path: str, _caption: str) -> None:
        self.notification.emit("Smoke post skipped", False)

    @Slot(str, result=str)
    def getThumbnail(self, _path: str) -> str:
        return ""

    @Slot(str, result=str)
    def getFileInfo(self, _path: str) -> str:
        return ""

    @Slot(str)
    def saveSettings(self, _settings: str) -> None:
        self.settingsSaved.emit(True, "Settings saved")

    @Slot(result=str)
    def getGitLog(self) -> str:
        return json.dumps({"ok": False, "commits": []})


def _make_engine(qml_dir: Path) -> tuple[QQmlApplicationEngine, ThemeProvider, SmokeController]:
    engine = QQmlApplicationEngine()
    engine.addImportPath(str(qml_dir))
    engine.addImportPath(str(qml_dir / "pages"))

    theme = ThemeProvider()
    controller = SmokeController()
    QQmlEngine.setObjectOwnership(theme, QQmlEngine.ObjectOwnership.CppOwnership)
    QQmlEngine.setObjectOwnership(controller, QQmlEngine.ObjectOwnership.CppOwnership)
    engine.rootContext().setContextProperty("theme", theme)
    engine.rootContext().setContextProperty("controller", controller)
    return engine, theme, controller


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    qml_dir = root / "src" / "xpst" / "desktop_app" / "qml"
    pages_dir = qml_dir / "pages"
    app = QApplication.instance() or QApplication([])
    failures: list[str] = []

    engine, _theme, _controller = _make_engine(qml_dir)
    main_qml = qml_dir / "main.qml"
    engine.load(QUrl.fromLocalFile(str(main_qml)))
    if not engine.rootObjects():
        failures.append(f"{main_qml}: no root objects")
    else:
        print(f"main.qml root_objects {len(engine.rootObjects())}")

    for page in sorted(pages_dir.glob("*.qml")):
        page_engine, _page_theme, _page_controller = _make_engine(qml_dir)
        component = QQmlComponent(page_engine, QUrl.fromLocalFile(str(page)))
        obj = component.create()
        if obj is None:
            errors = "; ".join(error.toString() for error in component.errors())
            failures.append(f"{page.name}: {errors}")
        else:
            print(f"{page.name} ok")
            obj.deleteLater()

    app.processEvents()
    if failures:
        print("QML smoke failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

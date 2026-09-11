import sys
from pathlib import Path

import pytest

QML_DIR = Path(__file__).parents[1] / "src/xpst/desktop_app/qml"


def test_qml_sidebar_click_navigates_compose():
    if sys.platform != "darwin":
        pytest.skip("native QML input regression is macOS-specific")

    from PySide6.QtCore import QPoint, Qt, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from xpst.desktop_app.backend import AppController, ThemeProvider
    from xpst.desktop_app.models import PostListModel

    app = QApplication.instance() or QApplication([])
    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR.parents[2]))
    engine.addImportPath(str(QML_DIR.parents[1]))
    engine.rootContext().setContextProperty("theme", ThemeProvider())
    engine.rootContext().setContextProperty("controller", AppController())
    engine.rootContext().setContextProperty("postModel", PostListModel())
    engine.rootContext().setContextProperty("xpstNoSplash", True)
    engine.rootContext().setContextProperty("macUnifiedTitlebar", False)
    engine.rootContext().setContextProperty("logoUrl", "")
    engine.rootContext().setContextProperty("iconFontUrl", "")
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "main.qml")))
    app.processEvents()
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    root.navigateTo("dashboard")
    app.processEvents()
    assert root.property("currentPage") == "dashboard"

    QTest.mouseClick(root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                      QPoint(200, 125))
    app.processEvents()
    assert root.property("currentPage") == "compose"
    root.close()


def test_macos_helper_disables_native_background_drag(monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("native AppKit helper is macOS-specific")

    import ctypes
    import ctypes.util

    from xpst.desktop_app import main as desktop_main

    class FakeSend:
        def __init__(self):
            self.calls = []
            self.restype = None
            self.argtypes = None

        def __call__(self, _receiver, selector, *args):
            self.calls.append((selector, args))
            if selector == "window":
                return 0x1000
            if selector == "styleMask":
                return 0
            return 0

    class FakeObjC:
        def __init__(self):
            self.objc_msgSend = FakeSend()

        @staticmethod
        def sel_registerName(selector):  # noqa: N802
            return selector.decode("utf-8")

    fake_objc = FakeObjC()

    class FakeWindow:
        def winId(self):  # noqa: N802
            return 123

    class FakeRoot:
        def windowHandle(self):  # noqa: N802
            return FakeWindow()

    class FakeEngine:
        def rootObjects(self):  # noqa: N802
            return [FakeRoot()]

    monkeypatch.setattr(desktop_main.sys, "platform", "darwin")
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: name)
    monkeypatch.setattr(ctypes.cdll, "LoadLibrary", lambda _name: fake_objc)

    desktop_main._make_macos_unified_window(FakeEngine())
    selectors = [selector for selector, _args in fake_objc.objc_msgSend.calls]
    assert "setTitlebarAppearsTransparent:" in selectors
    assert "setTitleVisibility:" in selectors
    movable_calls = [args for selector, args in fake_objc.objc_msgSend.calls
                     if selector == "setMovableByWindowBackground:"]
    assert movable_calls == [(False,)]

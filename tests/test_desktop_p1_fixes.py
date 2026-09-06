"""Regression coverage for the macOS desktop P1 fixes."""

from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
QML = REPO / "src/xpst/desktop_app/qml"


def test_visible_version_has_no_stale_ui_fallbacks():
    sidebar = (QML / "Sidebar.qml").read_text()
    about = (QML / "pages/AboutPage.qml").read_text()
    assert '"1.0.0"' not in sidebar
    assert '"1.0.0"' not in about
    assert "controller.appVersion" in sidebar
    assert "controller.appVersion" in about


def test_bundle_metadata_uses_project_version_and_embeds_source_sha():
    spec = (REPO / "build_macos.spec").read_text()
    assert "CFBundleShortVersionString\": project_version" in spec
    assert "CFBundleVersion\": project_version" in spec
    assert '"XPSTSourceCommit": source_commit' in spec
    assert '"NSQuitAlwaysKeepsWindows": False' in spec
    assert '"CFBundleShortVersionString": "0.1.0"' not in spec


def test_qml_window_and_navigation_expose_accessible_controls():
    main = (QML / "main.qml").read_text()
    sidebar = (QML / "Sidebar.qml").read_text()
    launcher = (REPO / "src/xpst/desktop_app/main.py").read_text()
    assert "Accessible.name: \"xPST Cross-Posting Suite\"" in main
    assert 'Accessible.id: "xpst-main-content"' in main
    assert 'Accessible.id: "nav-" + modelData.page' in sidebar
    assert "QAccessible.setActive(True)" in launcher
    assert "QAccessible.updateAccessibility" in launcher
    assert 'QT_ACCESSIBILITY", "1"' in launcher
    assert 'Accessible.name: modelData.label + " navigation"' in sidebar
    assert "Accessible.role: Accessible.Button" in sidebar


def test_build_script_prefers_checkout_python_sources():
    build = (REPO / "build.sh").read_text()
    assert 'export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"' in build


def test_window_visibility_helper_clamps_offscreen_bounds():
    # The CI "Test" step runs on Linux *before* the Qt/PySide6 runtime is
    # installed, and main.py raises SystemExit(1) at import when PySide6 is
    # absent. This helper is pure geometry, so skip rather than failing the
    # whole matrix on a missing runtime.
    try:
        from xpst.desktop_app.main import _window_needs_primary_placement as _helper
    except SystemExit:
        pytest.skip("PySide6/Qt runtime not available in this CI step")

    assert _helper((-1919, 0, 1280, 800), (0, 0, 1728, 1117))
    assert not _helper((200, 100, 1280, 800), (0, 0, 1728, 1117))

"""Regression coverage for the macOS desktop P1 fixes."""

from pathlib import Path

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
    assert "CFBundleShortVersionString\": PROJECT_VERSION" in spec
    assert "CFBundleVersion\": PROJECT_VERSION" in spec
    assert '"XPSTSourceSHA": SOURCE_SHA' in spec
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
    from xpst.desktop_app.main import _window_needs_primary_placement

    assert _window_needs_primary_placement((-1919, 0, 1280, 800), (0, 0, 1728, 1117))
    assert not _window_needs_primary_placement((200, 100, 1280, 800), (0, 0, 1728, 1117))

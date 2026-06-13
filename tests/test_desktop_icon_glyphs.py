"""Tests for the Qt-free icon-glyph mapping (W4-5).

The icon set is the single source of truth shared by ThemeProvider (Python) and
Icons.qml, so it must be assertable without PySide6 or a display.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from xpst.desktop_app import icon_glyphs as ig


def test_font_family_is_lucide():
    assert ig.ICON_FONT_FAMILY == "lucide"
    assert ig.UI_FONT_FAMILY == "Inter"


def test_icon_font_path_points_at_bundled_ttf():
    path = ig.icon_font_path()
    assert path.parts[-3:] == ("assets", "fonts", "lucide.ttf")
    assert path.exists(), f"bundled icon font missing at {path}"


def test_ui_font_path_points_at_bundled_inter_ttf():
    path = ig.ui_font_path()
    assert path.parts[-3:] == ("assets", "fonts", "Inter.ttf")
    assert path.exists(), f"bundled UI font missing at {path}"


def test_glyph_returns_single_pua_char():
    g = ig.glyph("youtube")
    assert g == chr(0xE485)
    assert len(g) == 1
    assert 0xE000 <= ord(g) <= 0xF8FF, "glyph should be in the Unicode Private Use Area"


def test_unknown_glyph_raises_keyerror():
    with pytest.raises(KeyError):
        ig.glyph("definitely-not-an-icon")


def test_glyph_map_is_complete_and_pua():
    m = ig.glyph_map()
    assert m, "glyph map must not be empty"
    assert set(m) == set(ig.ICON_CODEPOINTS)
    for name, ch in m.items():
        assert len(ch) == 1, name
        assert 0xE000 <= ord(ch) <= 0xF8FF, name
    # Returned dict is a fresh copy (mutating it must not corrupt the source).
    m.clear()
    assert ig.glyph_map(), "glyph_map must return a fresh dict each call"


def test_theme_provider_exposes_sidebar_nav_icons():
    pytest.importorskip("PySide6")

    from xpst.desktop_app.backend import ThemeProvider

    theme = ThemeProvider()
    assert theme.iconDashboard == ig.glyph("dashboard")
    assert theme.iconContent == ig.glyph("content")
    assert theme.iconAnalytics == ig.glyph("analytics")
    assert theme.iconViews == ig.glyph("views")
    assert theme.iconLikes == ig.glyph("likes")
    assert theme.iconComments == ig.glyph("comments")
    assert theme.iconShares == ig.glyph("shares")
    assert theme.iconViewGrid == ig.glyph("view_grid")
    assert theme.iconViewList == ig.glyph("view_list")
    assert theme.iconChevronLeft == ig.glyph("chevron_left")
    assert theme.iconChevronRight == ig.glyph("chevron_right")
    assert theme.iconConnect == ig.glyph("connect")
    assert theme.iconSchedule == ig.glyph("schedule")
    assert theme.iconSettings == ig.glyph("settings")
    assert theme.iconAbout == ig.glyph("about")
    assert theme.iconUsers == ig.glyph("users")
    assert theme.iconTrophy == ig.glyph("trophy")
    assert theme.iconCalendar == ig.glyph("calendar")
    assert theme.iconVideo == ig.glyph("video")
    assert theme.iconCheck == ig.glyph("check")
    assert theme.iconError == ig.glyph("error")
    assert theme.iconEdit == ig.glyph("edit")
    assert theme.iconWeb == ig.glyph("web")
    assert theme.iconRepo == ig.glyph("repo")
    assert theme.iconDocs == ig.glyph("docs")
    assert theme.iconIssue == ig.glyph("issue")
    assert theme.iconChangelog == ig.glyph("changelog")
    assert theme.iconPlus == ig.glyph("plus")
    assert theme.iconPlay == ig.glyph("play")
    assert theme.iconPause == ig.glyph("pause")
    assert theme.iconStop == ig.glyph("stop")
    assert theme.iconExternal == ig.glyph("external")


def test_qml_does_not_use_emoji_or_fake_chart_history():
    offenders = []
    disallowed = ("📊", "👥", "🏆", "📅", "Math.random", "simulated")
    for path in _qml_files():
        content = path.read_text(encoding="utf-8-sig")
        for token in disallowed:
            if token in content:
                offenders.append(f"{path.name}:{token}")
    assert not offenders, "unpolished/fake UI tokens remain: " + ", ".join(offenders)


def test_qml_avoids_text_as_icon_placeholders():
    offenders = []
    disallowed = (
        'text: "!"',
        'text: "OK"',
        'text: "v"',
        'text: "Video"',
        'text: "Calendar"',
        'text: "Empty"',
        'text: "Edit"',
        'text: "+ Schedule New"',
        'text: "<"',
        'text: ">"',
        'text: "< Previous"',
        'text: "Next >"',
        'return "YT"',
        'return "IG"',
        'return "TT"',
        'return "P"',
    )
    for path in _qml_files():
        content = path.read_text(encoding="utf-8-sig")
        for token in disallowed:
            if token in content:
                offenders.append(f"{path.name}:{token}")
    assert not offenders, "text-as-icon placeholders remain: " + ", ".join(offenders)


def test_sidebar_icon_glyphs_use_theme_colors():
    from pathlib import Path

    sidebar = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "Sidebar.qml"
    ).read_text(encoding="utf-8-sig")

    assert "text: theme.iconLogo" in sidebar
    assert "color: theme.textPrimary" in sidebar
    assert "text: modelData.icon" in sidebar
    assert (
        "color: sidebar.currentPage === modelData.page ? theme.accent : theme.textSecondary"
        in sidebar
    )


def test_sidebar_notification_popup_uses_capped_height_for_position():
    from pathlib import Path

    sidebar = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "Sidebar.qml"
    ).read_text(encoding="utf-8-sig")

    assert "height: Math.min(360, notifPopupContent.implicitHeight + theme.spacingXl)" in sidebar
    assert "y: -height - 8" in sidebar
    assert "y: -notifPopupContent.implicitHeight - 8" not in sidebar


def test_analytics_uses_theme_icons_for_empty_state_and_metrics():
    from pathlib import Path

    analytics = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "AnalyticsPage.qml"
    )
    content = analytics.read_text(encoding="utf-8-sig")

    assert 'text: "A"' not in content
    for token in ('icon: "V"', 'icon: "L"', 'icon: "C"', 'icon: "S"'):
        assert token not in content
    for token in (
        "text: theme.iconAnalytics",
        "function platformIcon",
        "text: analyticsPage.platformIcon(modelData.platform)",
        "font.family: theme.iconFontFamily",
        "icon: theme.iconViews",
        "icon: theme.iconLikes",
        "icon: theme.iconComments",
        "icon: theme.iconShares",
    ):
        assert token in content


def test_content_view_toggle_uses_theme_icons():
    from pathlib import Path

    content_page = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ContentPage.qml"
    )
    content = content_page.read_text(encoding="utf-8-sig")

    assert 'text: "⊞"' not in content
    assert 'text: "List"' not in content
    assert 'text: "v"' not in content
    assert "text: theme.iconViewGrid" in content
    assert "text: theme.iconViewList" in content
    assert "text: theme.iconChevronRight" in content
    assert "rotation: 90" in content


def test_content_empty_and_edit_controls_use_theme_icons():
    from pathlib import Path

    content_page = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ContentPage.qml"
    )
    content = content_page.read_text(encoding="utf-8-sig")

    assert 'text: "Empty"' not in content
    assert 'text: "Edit"' not in content
    assert "text: theme.iconVideo" in content
    assert "text: theme.iconEdit" in content


def test_content_grid_uses_responsive_columns_and_fixed_thumbnail():
    from pathlib import Path

    content_page = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ContentPage.qml"
    )
    content = content_page.read_text(encoding="utf-8-sig")

    assert "contentPage.width < 900 ? 2 : 3" in content
    assert "Layout.preferredWidth: contentPage.listViewMode ? 56 : 72" in content
    assert "Layout.fillWidth: false" in content
    assert "Layout.alignment: Qt.AlignTop" in content


def test_content_delete_uses_source_id_and_row_selection_keys():
    from pathlib import Path

    content_page = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ContentPage.qml"
    )
    content = content_page.read_text(encoding="utf-8-sig")

    assert "readonly property int sourceIdRole: 264" in content
    assert "sourceId: sourceId" in content
    assert 'rowKey: sourceId + "::" + platform + "::" + postId' in content
    assert "controller.deletePost(posts[i].sourceId || posts[i].postId" in content
    assert "toggleSelection(modelData.rowKey || \"\")" in content
    assert "from all platforms" not in content


def test_content_caption_editor_updates_source_id():
    from pathlib import Path

    content_page = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ContentPage.qml"
    )
    content = content_page.read_text(encoding="utf-8-sig")

    assert "contentPage.editingPostId = modelData.sourceId || modelData.postId || \"\"" in content
    assert "controller.updateCaption(contentPage.editingPostId, plat, caps[plat])" in content


def test_content_video_preview_controls_use_theme_icons():
    from pathlib import Path

    content_page = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ContentPage.qml"
    )
    content = content_page.read_text(encoding="utf-8-sig")

    for token in ("⏸", "▶", "⏹", "↗"):
        assert token not in content
    for token in (
        "theme.iconPause",
        "theme.iconPlay",
        "theme.iconStop",
        "theme.iconExternal",
        "font.family: theme.iconFontFamily",
    ):
        assert token in content


def test_navigation_arrows_use_theme_icons():
    from pathlib import Path

    qml_root = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
    )
    content = (qml_root / "ContentPage.qml").read_text(encoding="utf-8-sig")
    schedule = (qml_root / "SchedulePage.qml").read_text(encoding="utf-8-sig")
    about = (qml_root / "AboutPage.qml").read_text(encoding="utf-8-sig")

    assert "text: theme.iconChevronLeft" in content
    assert "text: theme.iconChevronRight" in content
    assert "text: theme.iconChevronLeft" in schedule
    assert "text: theme.iconChevronRight" in schedule
    assert "text: theme.iconChevronRight" in about


def test_about_links_use_theme_icons():
    from pathlib import Path

    about = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "AboutPage.qml"
    ).read_text(encoding="utf-8-sig")

    for token in ("Web", "Repo", "Docs", "Issue", "Log"):
        assert f'icon: "{token}"' not in about
    for token in (
        "icon: theme.iconWeb",
        "icon: theme.iconRepo",
        "icon: theme.iconDocs",
        "icon: theme.iconIssue",
        "icon: theme.iconChangelog",
    ):
        assert token in about


def test_connect_onboarding_checkboxes_use_theme_text():
    from pathlib import Path

    connect = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ConnectPage.qml"
    ).read_text(encoding="utf-8-sig")

    assert connect.count("contentItem: Text") >= 3
    assert connect.count("color: theme.textPrimary") >= 3
    assert connect.count("indicator: Rectangle") >= 3
    assert connect.count("text: theme.iconCheck") >= 3
    for color in ("theme.youtube", "theme.instagram", "theme.xtwitter"):
        assert f"color: parent.checked ? {color} : theme.surfaceAlt" in connect
    for label in ('text: "YouTube"', 'text: "Instagram"', 'text: "X"'):
        assert label in connect


def test_schedule_platform_checkboxes_use_themed_indicators():
    from pathlib import Path

    schedule = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "SchedulePage.qml"
    ).read_text(encoding="utf-8-sig")

    assert schedule.count("CheckBox {") == 3
    assert schedule.count("indicator: Rectangle") >= 3
    assert schedule.count("text: theme.iconCheck") >= 3
    for color in ("theme.youtube", "theme.instagram", "theme.xtwitter"):
        assert f"color: parent.checked ? {color} : theme.surfaceAlt" in schedule


def test_settings_switches_use_themed_indicators():
    from pathlib import Path

    settings = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "SettingsPage.qml"
    ).read_text(encoding="utf-8-sig")

    assert settings.count("Switch {") == 3
    assert settings.count("indicator: Rectangle") >= 3
    assert settings.count("color: parent.checked ? theme.accent : theme.surfaceAlt") >= 2
    assert "color: parent.checked ? modelData.color : theme.surfaceAlt" in settings


def test_settings_download_dir_round_trips_as_video_config():
    from pathlib import Path

    settings = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "SettingsPage.qml"
    ).read_text(encoding="utf-8-sig")

    assert "downloadDir = cfg.video.download_dir || \"\"" in settings
    assert "video: {" in settings
    assert "download_dir: downloadDir" in settings
    assert "settings.download_dir = downloadDir" not in settings


def test_settings_notification_switches_round_trip_config():
    from pathlib import Path

    settings = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "SettingsPage.qml"
    ).read_text(encoding="utf-8-sig")

    assert "property bool postCompletionAlerts" in settings
    assert "property bool errorNotifications" in settings
    assert "postCompletionAlerts = cfg.notifications.enabled !== false" in settings
    assert "errorNotifications = cfg.notifications.enabled !== false" in settings
    assert "notifications: {" in settings
    assert "on_success: postCompletionAlerts" in settings
    assert "on_failure: errorNotifications" in settings
    assert "checked: true" not in settings


def test_settings_rate_limit_ui_matches_daily_config():
    from pathlib import Path

    settings = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "SettingsPage.qml"
    ).read_text(encoding="utf-8-sig")

    assert "Daily Upload Limit" in settings
    assert "Window Duration" not in settings
    assert "rateLimitMinutes" not in settings
    assert "rateLimitMinutesError" not in settings


def test_connect_x_cookie_dialog_checks_save_result():
    from pathlib import Path

    connect = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ConnectPage.qml"
    ).read_text(encoding="utf-8-sig")

    assert "var settings = { x_cookies: cookieInput.text }" in connect
    assert "var result = JSON.parse(raw)" in connect
    assert "if (result.ok)" in connect
    assert "result.error || \"Could not save X cookies\"" in connect
    assert 'showToast("X cookies saved", false)' in connect


def test_connect_setup_loads_saved_choices_from_config():
    from pathlib import Path

    connect = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "pages"
        / "ConnectPage.qml"
    ).read_text(encoding="utf-8-sig")

    assert "function loadSavedChoices()" in connect
    assert "connectPage.loadSavedChoices()" in connect
    assert "var cfg = JSON.parse(controller.configData)" in connect
    assert "connectPage.onboardingLocalPath = cfg.local.path || \"\"" in connect
    assert "connectPage.onboardingTikTokUsername = cfg.tiktok.username || \"\"" in connect
    assert "connectPage.onboardingYouTube = cfg.youtube.enabled" in connect
    assert "connectPage.onboardingInstagram = cfg.instagram.enabled" in connect
    assert "connectPage.onboardingX = cfg.x.enabled" in connect


def test_toast_text_is_bounded_and_wrapped():
    from pathlib import Path

    main_qml = (
        Path(__file__).parent.parent
        / "src"
        / "xpst"
        / "desktop_app"
        / "qml"
        / "main.qml"
    ).read_text(encoding="utf-8-sig")

    assert "width: Math.min(toastText.implicitWidth + 48, root.width - 64)" in main_qml
    assert "height: Math.max(44, toastText.implicitHeight + 20)" in main_qml
    assert "width: parent.width - 32" in main_qml
    assert "wrapMode: Text.WordWrap" in main_qml
    assert "maximumLineCount: 3" in main_qml
    assert "elide: Text.ElideRight" in main_qml


def test_icon_glyphs_is_qt_free():
    code = (
        "import sys; from xpst.desktop_app import icon_glyphs as ig; "
        "ig.glyph('youtube'); ig.glyph_map(); ig.icon_font_path(); ig.ui_font_path(); "
        "assert not [n for n in sys.modules if n.split('.')[0] == 'PySide6']; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# ── QML glyph lint (G38) ─────────────────────────────────────────────


def _qml_files():
    from pathlib import Path

    root = Path(__file__).parent.parent / "src" / "xpst" / "desktop_app" / "qml"
    return sorted(root.rglob("*.qml"))


def _text_blocks(content: str):
    """Yield (start_line, block_text) for each Text { ... } block, naive
    brace matcher — good enough for lint purposes."""
    lines = content.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Text {") or stripped == "Text{":
            depth = 0
            block: list[str] = []
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                block.append(lines[j])
                if depth <= 0:
                    break
            yield i + 1, "\n".join(block)


def test_qml_glyph_lint():
    """Any Text whose content comes from an icon source (``.icon``,
    ``providerIcon(``, ``Icons.``, ``glyph(``) must set ``font.family`` —
    otherwise the PUA codepoint renders as tofu (G38). This is the
    regression guard for the AnalyticsPage:205 class of bug."""
    icon_markers = (".icon", "providerIcon(", "Icons.", "glyph(")
    offenders = []
    for path in _qml_files():
        content = path.read_text(encoding="utf-8-sig")
        for line_no, block in _text_blocks(content):
            text_lines = [
                ln for ln in block.splitlines() if ln.strip().startswith("text:")
            ]
            if not text_lines:
                continue
            uses_icon = any(
                marker in ln for ln in text_lines for marker in icon_markers
            )
            if uses_icon and "font.family" not in block:
                offenders.append(f"{path.name}:{line_no}")
    assert not offenders, (
        "icon-glyph Text without font.family (renders as tofu): "
        + ", ".join(offenders)
    )


def test_no_glyph_concatenated_into_labels():
    """Icon glyphs must never be string-concatenated into a default-font
    label (``modelData.icon + modelData.name``) — split layouts only."""
    offenders = []
    for path in _qml_files():
        content = path.read_text(encoding="utf-8-sig")
        for i, line in enumerate(content.splitlines(), 1):
            if ".icon +" in line or "+ modelData.icon" in line:
                offenders.append(f"{path.name}:{i}")
    assert not offenders, "glyph concatenated into label: " + ", ".join(offenders)

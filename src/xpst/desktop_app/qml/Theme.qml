pragma Singleton
import QtQuick 2.15

QtObject {
    // Canvas / Background
    readonly property color canvas: "#0a0a0f"
    readonly property color surface: "#12121a"
    readonly property color surfaceAlt: "#1a1a25"
    readonly property color surfaceCard: "#1e1e2a"

    // Text
    readonly property color textPrimary: "#f0f0f5"
    readonly property color textSecondary: "#a0a0b0"
    readonly property color textMuted: "#6b6b80"

    // Accent
    readonly property color accent: "#6366f1"
    readonly property color accentHover: "#818cf8"
    readonly property color accentMuted: "#312e81"

    // Status
    readonly property color success: "#22c55e"
    readonly property color warning: "#f59e0b"
    readonly property color error: "#ef4444"

    // Platform
    readonly property color youtube: "#ff0000"
    readonly property color instagram: "#e1306c"
    readonly property color xPlatform: "#1d9bf0"
    readonly property color tiktok: "#00f2ea"

    // Spacing
    readonly property int spacing4: 4
    readonly property int spacing8: 8
    readonly property int spacing12: 12
    readonly property int spacing16: 16
    readonly property int spacing24: 24
    readonly property int spacing32: 32

    // Radius
    readonly property int radius6: 6
    readonly property int radius8: 8
    readonly property int radius12: 12
    readonly property int radius16: 16

    // Font sizes
    readonly property int fontXs: 11
    readonly property int fontSm: 12
    readonly property int fontMd: 14
    readonly property int fontLg: 16
    readonly property int fontXl: 20
    readonly property int font2Xl: 26
    readonly property int font3Xl: 32

    // Light theme overrides (toggleable)
    property bool darkMode: true

    readonly property color effectiveCanvas: darkMode ? canvas : "#f5f5f8"
    readonly property color effectiveSurface: darkMode ? surface : "#ffffff"
    readonly property color effectiveSurfaceAlt: darkMode ? surfaceAlt : "#eeeef2"
    readonly property color effectiveSurfaceCard: darkMode ? surfaceCard : "#ffffff"
    readonly property color effectiveTextPrimary: darkMode ? textPrimary : "#1a1a25"
    readonly property color effectiveTextSecondary: darkMode ? textSecondary : "#555565"
    readonly property color effectiveTextMuted: darkMode ? textMuted : "#888899"
}

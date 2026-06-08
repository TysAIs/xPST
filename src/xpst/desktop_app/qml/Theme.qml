pragma Singleton
import QtQuick 2.15

QtObject {
    property bool darkMode: true

    readonly property string fontFamily: Qt.platform.os === "osx" ? ".AppleSystemUIFont" : "Segoe UI Variable"
    readonly property string monoFamily: Qt.platform.os === "osx" ? "SF Mono" : "Cascadia Mono"

    // Apple-like neutral surfaces. The app defaults dark, but avoids the old
    // purple-heavy palette and keeps contrast quieter.
    readonly property color canvas: darkMode ? "#101114" : "#f5f5f7"
    readonly property color surface: darkMode ? "#17181c" : "#ffffff"
    readonly property color surfaceAlt: darkMode ? "#202126" : "#f0f1f4"
    readonly property color surfaceCard: darkMode ? "#1b1c21" : "#ffffff"
    readonly property color elevated: darkMode ? "#24262c" : "#ffffff"
    readonly property color separator: darkMode ? "#2d3037" : "#d8d9de"
    readonly property color glass: darkMode ? "#16171bcc" : "#fffffff0"

    readonly property color textPrimary: darkMode ? "#f4f4f6" : "#1d1d1f"
    readonly property color textSecondary: darkMode ? "#b7bac2" : "#51545d"
    readonly property color textMuted: darkMode ? "#7a7f8c" : "#7a7d86"

    readonly property color accent: "#0a84ff"
    readonly property color accentHover: "#409cff"
    readonly property color accentMuted: darkMode ? "#0a84ff24" : "#0a84ff18"

    readonly property color success: "#30d158"
    readonly property color warning: "#ff9f0a"
    readonly property color error: "#ff453a"

    readonly property color youtube: "#ff3b30"
    readonly property color instagram: "#bf5af2"
    readonly property color xPlatform: "#64d2ff"
    readonly property color xtwitter: xPlatform
    readonly property color tiktok: "#5eead4"

    readonly property int spacingXs: 4
    readonly property int spacingSm: 8
    readonly property int spacingMd: 12
    readonly property int spacingLg: 16
    readonly property int spacingXl: 24
    readonly property int spacingXxl: 32
    readonly property int pageMargin: 34

    readonly property int spacing4: spacingXs
    readonly property int spacing8: spacingSm
    readonly property int spacing12: spacingMd
    readonly property int spacing16: spacingLg
    readonly property int spacing24: spacingXl
    readonly property int spacing32: spacingXxl

    readonly property int radiusSm: 6
    readonly property int radiusMd: 8
    readonly property int radiusLg: 10
    readonly property int radiusXl: 12
    readonly property int radius6: radiusSm
    readonly property int radius8: radiusMd
    readonly property int radius12: radiusLg
    readonly property int radius16: radiusXl

    readonly property int fontXs: 11
    readonly property int fontSm: 12
    readonly property int fontMd: 13
    readonly property int fontLg: 15
    readonly property int fontXl: 20
    readonly property int font2Xl: 28
    readonly property int font3Xl: 34

    readonly property color effectiveCanvas: canvas
    readonly property color effectiveSurface: surface
    readonly property color effectiveSurfaceAlt: surfaceAlt
    readonly property color effectiveSurfaceCard: surfaceCard
    readonly property color effectiveTextPrimary: textPrimary
    readonly property color effectiveTextSecondary: textSecondary
    readonly property color effectiveTextMuted: textMuted
}

pragma Singleton
import QtQuick 2.15

QtObject {
    property bool darkMode: false

    readonly property color canvas: darkMode ? "#1c1c1e" : "#f5f5f7"
    readonly property color surface: darkMode ? "#242426" : "#fbfbfd"
    readonly property color surfaceAlt: darkMode ? "#2c2c2e" : "#f0f0f2"
    readonly property color surfaceCard: darkMode ? "#2c2c2e" : "#ffffff"

    readonly property color textPrimary: darkMode ? "#f5f5f7" : "#1d1d1f"
    readonly property color textSecondary: darkMode ? "#c7c7cc" : "#5f6368"
    readonly property color textMuted: darkMode ? "#8e8e93" : "#8a8f98"

    readonly property color accent: "#007aff"
    readonly property color accentHover: "#0a84ff"
    readonly property color accentMuted: darkMode ? "#0f355f" : "#dbeafe"

    readonly property color success: darkMode ? "#30d158" : "#248a3d"
    readonly property color warning: darkMode ? "#ffd60a" : "#b26a00"
    readonly property color error: darkMode ? "#ff453a" : "#d70015"

    readonly property color youtube: "#ff0033"
    readonly property color instagram: "#c13584"
    readonly property color xtwitter: "#1d9bf0"
    readonly property color xPlatform: xtwitter
    readonly property color tiktok: "#00b8b8"

    readonly property int spacingXs: 4
    readonly property int spacingSm: 8
    readonly property int spacingMd: 12
    readonly property int spacingLg: 16
    readonly property int spacingXl: 24
    readonly property int spacingXxl: 32
    readonly property int pageMargin: 28

    readonly property int radiusSm: 4
    readonly property int radiusMd: 6
    readonly property int radiusLg: 8
    readonly property int radiusXl: 8

    readonly property string fontFamily: "Segoe UI"
    readonly property string monoFontFamily: "Cascadia Mono"
}

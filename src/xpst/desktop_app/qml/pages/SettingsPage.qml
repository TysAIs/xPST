import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: settingsPage
    background: Rectangle { color: theme.canvas }

    // ── Settings properties ─────────────────────────────────────
    property string tiktokUsername: ""
    property string downloadDir: ""
    property bool youtubeEnabled: true
    property bool instagramEnabled: true
    property bool xEnabled: true
    property bool tiktokEnabled: true
    property int rateLimitPosts: 10
    property int rateLimitMinutes: 60

    // ── Validation state ────────────────────────────────────────
    property bool hasErrors: false
    property string rateLimitPostsError: ""
    property string rateLimitMinutesError: ""
    property string downloadDirError: ""

    function validateForm() {
        var errors = false

        // Rate limit posts: must be positive integer
        if (rateLimitPosts <= 0 || isNaN(rateLimitPosts)) {
            rateLimitPostsError = "Must be a positive number"
            errors = true
        } else {
            rateLimitPostsError = ""
        }

        // Rate limit minutes: must be positive integer
        if (rateLimitMinutes <= 0 || isNaN(rateLimitMinutes)) {
            rateLimitMinutesError = "Must be a positive number"
            errors = true
        } else {
            rateLimitMinutesError = ""
        }

        // Download directory: basic path validation
        if (downloadDir.length > 0) {
            // Check for invalid characters (simplified)
            var invalidChars = /[<>"|?*]/
            if (invalidChars.test(downloadDir)) {
                downloadDirError = "Contains invalid characters"
                errors = true
            } else {
                downloadDirError = ""
            }
        } else {
            downloadDirError = ""
        }

        hasErrors = errors
        return !errors
    }

    // ── Load from controller on creation ────────────────────────
    Component.onCompleted: loadFromConfig()

    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onDataChanged() {
            loadFromConfig()
        }
    }

    function loadFromConfig() {
        if (typeof controller === "undefined") return
        try {
            var cfg = JSON.parse(controller.configData)
            if (cfg.error) return

            // Load per-platform settings
            if (cfg.youtube) {
                youtubeEnabled = cfg.youtube.enabled !== false
            }
            if (cfg.instagram) {
                instagramEnabled = cfg.instagram.enabled !== false
            }
            if (cfg.x) {
                xEnabled = cfg.x.enabled !== false
            }
            if (cfg.tiktok) {
                tiktokEnabled = cfg.tiktok.enabled !== false
                tiktokUsername = cfg.tiktok.username || ""
            }

            // Load rate limits
            if (cfg.rate_limits) {
                // Use the max of all platform limits
                var maxRate = 10
                for (var p in cfg.rate_limits) {
                    var v = cfg.rate_limits[p]
                    if (typeof v === "number" && v > 0) maxRate = Math.max(maxRate, v)
                }
                rateLimitPosts = maxRate
            }

            // Load monitoring
            if (cfg.monitoring) {
                // log_level not directly mapped to UI yet
            }
        } catch(e) {
            console.log("Settings load error:", e)
        }
    }

    function saveSettings() {
        if (!validateForm()) return
        if (typeof controller === "undefined") return

        var settings = {
            youtube: { enabled: youtubeEnabled },
            instagram: { enabled: instagramEnabled },
            x: { enabled: xEnabled },
            tiktok: { enabled: tiktokEnabled, username: tiktokUsername },
            rate_limits: {
                youtube: rateLimitPosts,
                instagram: rateLimitPosts,
                x: rateLimitPosts,
                tiktok: rateLimitPosts
            },
            monitoring: {
                log_level: "INFO"
            }
        }

        if (downloadDir.length > 0) {
            settings.download_dir = downloadDir
        }

        controller.saveSettings(JSON.stringify(settings))
    }

    function resetSettings() {
        loadFromConfig()
        rateLimitPostsError = ""
        rateLimitMinutesError = ""
        downloadDirError = ""
        hasErrors = false
    }

    function closeDialog() {
        // Stub for keyboard shortcut escape handling
    }

    Flickable {
        anchors.fill: parent
        contentHeight: settingsCol.implicitHeight + theme.spacingXxl
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: settingsCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: theme.spacingXl
            spacing: theme.spacingXl

            // Header
            Text {
                text: "Settings"
                font.pixelSize: 20
                font.bold: true
                color: theme.textPrimary
            }

            // General Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "General"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: generalCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: generalCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingXl

                        ColumnLayout {
                            spacing: theme.spacingXs
                            Text {
                                text: "TikTok Username"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1
                                TextInput {
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingMd
                                    text: settingsPage.tiktokUsername
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    clip: true
                                    onTextChanged: settingsPage.tiktokUsername = text
                                }
                            }
                        }

                        ColumnLayout {
                            spacing: theme.spacingXs
                            Text {
                                text: "Download Directory"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: downloadDirError.length > 0 ? theme.error : theme.textMuted
                                border.width: downloadDirError.length > 0 ? 2 : 1
                                TextInput {
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingMd
                                    text: settingsPage.downloadDir
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    clip: true
                                    onTextChanged: {
                                        settingsPage.downloadDir = text
                                        settingsPage.validateForm()
                                    }
                                }
                            }
                            Text {
                                text: downloadDirError
                                font.pixelSize: 11
                                color: theme.error
                                visible: downloadDirError.length > 0
                            }
                        }
                    }
                }
            }

            // Platforms Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Platforms"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: platformsCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: platformsCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingMd

                        Repeater {
                            model: [
                                { label: "YouTube",   prop: "youtubeEnabled",   color: theme.youtube },
                                { label: "Instagram", prop: "instagramEnabled", color: theme.instagram },
                                { label: "X",         prop: "xEnabled",         color: theme.xtwitter },
                                { label: "TikTok",    prop: "tiktokEnabled",    color: theme.tiktok }
                            ]

                            RowLayout {
                                spacing: theme.spacingMd
                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    color: modelData.color
                                }
                                Text {
                                    text: modelData.label
                                    font.pixelSize: 13
                                    color: theme.textPrimary
                                    Layout.fillWidth: true
                                }
                                Switch {
                                    checked: settingsPage[modelData.prop]
                                    onCheckedChanged: settingsPage[modelData.prop] = checked
                                }
                            }
                        }
                    }
                }
            }

            // Rate Limits Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Rate Limits"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: rateCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: rateCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingXl

                        ColumnLayout {
                            spacing: theme.spacingXs
                            Text {
                                text: "Max Posts per Window"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: rateLimitPostsError.length > 0 ? theme.error : theme.textMuted
                                border.width: rateLimitPostsError.length > 0 ? 2 : 1
                                TextInput {
                                    id: rateLimitPostsInput
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingMd
                                    text: String(settingsPage.rateLimitPosts)
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    clip: true
                                    onTextChanged: {
                                        var v = parseInt(text)
                                        if (!isNaN(v)) settingsPage.rateLimitPosts = v
                                        settingsPage.validateForm()
                                    }
                                }
                            }
                            Text {
                                text: rateLimitPostsError
                                font.pixelSize: 11
                                color: theme.error
                                visible: rateLimitPostsError.length > 0
                            }
                        }

                        ColumnLayout {
                            spacing: theme.spacingXs
                            Text {
                                text: "Window Duration (minutes)"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: rateLimitMinutesError.length > 0 ? theme.error : theme.textMuted
                                border.width: rateLimitMinutesError.length > 0 ? 2 : 1
                                TextInput {
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingMd
                                    text: String(settingsPage.rateLimitMinutes)
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    clip: true
                                    onTextChanged: {
                                        var v = parseInt(text)
                                        if (!isNaN(v)) settingsPage.rateLimitMinutes = v
                                        settingsPage.validateForm()
                                    }
                                }
                            }
                            Text {
                                text: rateLimitMinutesError
                                font.pixelSize: 11
                                color: theme.error
                                visible: rateLimitMinutesError.length > 0
                            }
                        }
                    }
                }
            }

            // Notifications Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Notifications"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: notifCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: notifCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingMd

                        RowLayout {
                            Text {
                                text: "Post completion alerts"
                                font.pixelSize: 13
                                color: theme.textPrimary
                                Layout.fillWidth: true
                            }
                            Switch { checked: true }
                        }
                        RowLayout {
                            Text {
                                text: "Error notifications"
                                font.pixelSize: 13
                                color: theme.textPrimary
                                Layout.fillWidth: true
                            }
                            Switch { checked: true }
                        }
                    }
                }
            }

            // Buttons
            RowLayout {
                spacing: theme.spacingMd
                Layout.topMargin: theme.spacingSm

                Rectangle {
                    width: saveLabel.implicitWidth + theme.spacingXxl
                    height: 40
                    radius: theme.radiusMd
                    color: hasErrors ? theme.surfaceAlt : theme.accent
                    opacity: hasErrors ? 0.6 : 1.0

                    Text {
                        id: saveLabel
                        anchors.centerIn: parent
                        text: "Save Settings"
                        font.pixelSize: 13
                        font.bold: true
                        color: hasErrors ? theme.textMuted : "#ffffff"
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: hasErrors ? Qt.ForbiddenCursor : Qt.PointingHandCursor
                        enabled: !hasErrors
                        onClicked: settingsPage.saveSettings()
                    }
                }

                Rectangle {
                    width: resetLabel.implicitWidth + theme.spacingXxl
                    height: 40
                    radius: theme.radiusMd
                    color: theme.surfaceAlt

                    Text {
                        id: resetLabel
                        anchors.centerIn: parent
                        text: "Reset"
                        font.pixelSize: 13
                        color: theme.textSecondary
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: settingsPage.resetSettings()
                    }
                }

                Item { Layout.fillWidth: true }
            }
        }
    }
}

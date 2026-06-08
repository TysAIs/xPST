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
    property bool mcpRunning: false

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
                                    Accessible.name: "TikTok Username input"
                                    Accessible.role: Accessible.EditableText
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
                                    Accessible.name: "Download Directory input"
                                    Accessible.role: Accessible.EditableText
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
                                    Accessible.name: "Enable " + modelData.label
                                    Accessible.role: Accessible.CheckBox
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
                                    Accessible.name: "Max Posts per Window input"
                                    Accessible.role: Accessible.EditableText
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
                                    Accessible.name: "Window Duration input"
                                    Accessible.role: Accessible.EditableText
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
                            Switch {
                                checked: true
                                Accessible.name: "Toggle post completion alerts"
                                Accessible.role: Accessible.CheckBox
                            }
                        }
                        RowLayout {
                            Text {
                                text: "Error notifications"
                                font.pixelSize: 13
                                color: theme.textPrimary
                                Layout.fillWidth: true
                            }
                            Switch {
                                checked: true
                                Accessible.name: "Toggle error notifications"
                                Accessible.role: Accessible.CheckBox
                            }
                        }
                    }
                }
            }

            // Encoding Preview Section (Item 10)
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Encoding Preview"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Text {
                    text: "Current encoding parameters per platform"
                    font.pixelSize: 12
                    color: theme.textSecondary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: encCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: encCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingMd

                        Repeater {
                            model: [
                                {
                                    platform: "YouTube",
                                    codec: "H.264 / AVC",
                                    resolution: "1080p (1920×1080)",
                                    fps: "30 fps",
                                    bitrate: "8-12 Mbps",
                                    audioCodec: "AAC",
                                    audioBitrate: "256 kbps",
                                    container: "MP4",
                                    maxDuration: "60s (Shorts)",
                                    color: theme.youtube
                                },
                                {
                                    platform: "Instagram",
                                    codec: "H.264 / AVC",
                                    resolution: "1080×1920 (9:16)",
                                    fps: "30 fps",
                                    bitrate: "5-8 Mbps",
                                    audioCodec: "AAC",
                                    audioBitrate: "128 kbps",
                                    container: "MP4",
                                    maxDuration: "90s (Reels)",
                                    color: theme.instagram
                                },
                                {
                                    platform: "X",
                                    codec: "H.264 / AVC",
                                    resolution: "1280×720 (16:9)",
                                    fps: "30 fps",
                                    bitrate: "5-8 Mbps",
                                    audioCodec: "AAC",
                                    audioBitrate: "128 kbps",
                                    container: "MP4",
                                    maxDuration: "140s",
                                    color: theme.xtwitter
                                },
                                {
                                    platform: "TikTok",
                                    codec: "H.264 / AVC",
                                    resolution: "1080×1920 (9:16)",
                                    fps: "30 fps",
                                    bitrate: "4-6 Mbps",
                                    audioCodec: "AAC",
                                    audioBitrate: "128 kbps",
                                    container: "MP4",
                                    maxDuration: "60s",
                                    color: theme.tiktok
                                }
                            ]

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: encPlatformCol.implicitHeight + theme.spacingXl
                                radius: theme.radiusMd
                                color: theme.surfaceAlt

                                ColumnLayout {
                                    id: encPlatformCol
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingLg
                                    spacing: theme.spacingSm

                                    RowLayout {
                                        spacing: theme.spacingSm
                                        Rectangle {
                                            width: 8; height: 8; radius: 4
                                            color: modelData.color
                                        }
                                        Text {
                                            text: modelData.platform
                                            font.pixelSize: 13
                                            font.bold: true
                                            color: theme.textPrimary
                                        }
                                        Item { Layout.fillWidth: true }
                                    }

                                    GridLayout {
                                        columns: 4
                                        columnSpacing: theme.spacingXl
                                        rowSpacing: theme.spacingXs
                                        Layout.fillWidth: true

                                        Text { text: "Codec"; font.pixelSize: 11; color: theme.textMuted }
                                        Text { text: modelData.codec; font.pixelSize: 11; color: theme.textSecondary }

                                        Text { text: "Resolution"; font.pixelSize: 11; color: theme.textMuted }
                                        Text { text: modelData.resolution; font.pixelSize: 11; color: theme.textSecondary }

                                        Text { text: "FPS"; font.pixelSize: 11; color: theme.textMuted }
                                        Text { text: modelData.fps; font.pixelSize: 11; color: theme.textSecondary }

                                        Text { text: "Bitrate"; font.pixelSize: 11; color: theme.textMuted }
                                        Text { text: modelData.bitrate; font.pixelSize: 11; color: theme.textSecondary }

                                        Text { text: "Audio"; font.pixelSize: 11; color: theme.textMuted }
                                        Text { text: modelData.audioCodec + " " + modelData.audioBitrate; font.pixelSize: 11; color: theme.textSecondary }

                                        Text { text: "Container"; font.pixelSize: 11; color: theme.textMuted }
                                        Text { text: modelData.container; font.pixelSize: 11; color: theme.textSecondary }

                                        Text { text: "Max Duration"; font.pixelSize: 11; color: theme.textMuted }
                                        Text { text: modelData.maxDuration; font.pixelSize: 11; color: theme.textSecondary }
                                    }
                                }
                            }
                        }
                    }
                }
            }

    // Developer / MCP Server Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Developer"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                    Accessible.name: "Developer settings section"
                    Accessible.role: Accessible.Heading
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: mcpCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: mcpCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingMd

                        Text {
                            text: "MCP Server"
                            font.pixelSize: 14
                            font.bold: true
                            color: theme.textPrimary
                        }

                        RowLayout {
                            spacing: theme.spacingMd
                            Rectangle {
                                width: 8; height: 8; radius: 4
                                color: mcpRunning ? theme.success : theme.error
                            }
                            Text {
                                text: mcpRunning ? "Running" : "Stopped"
                                font.pixelSize: 13
                                color: theme.textSecondary
                            }
                            Item { Layout.fillWidth: true }

                            Rectangle {
                                width: mcpBtnLabel.implicitWidth + theme.spacingXxl
                                height: 32
                                radius: theme.radiusMd
                                color: mcpRunning ? theme.error : theme.accent
                                opacity: mcpBtnMouse.containsMouse ? 0.85 : 1.0

                                Text {
                                    id: mcpBtnLabel
                                    anchors.centerIn: parent
                                    text: mcpRunning ? "Stop" : "Start"
                                    font.pixelSize: 12
                                    font.bold: true
                                    color: "#ffffff"
                                }

                                MouseArea {
                                    id: mcpBtnMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        mcpRunning = !mcpRunning
                                        showToast(mcpRunning ? "MCP server started" : "MCP server stopped", false)
                                    }
                                    Accessible.name: mcpRunning ? "Stop MCP server" : "Start MCP server"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }

                        // MCP Tools list
                        Text {
                            text: "Available Tools"
                            font.pixelSize: 12
                            color: theme.textMuted
                            visible: mcpRunning
                        }

                        ColumnLayout {
                            spacing: theme.spacingXs
                            visible: mcpRunning

                            Repeater {
                                model: [
                                    { name: "post_video", desc: "Post video to platforms" },
                                    { name: "crosspost_new", desc: "Cross-post new content" },
                                    { name: "check_status", desc: "Check system status" },
                                    { name: "list_platforms", desc: "List configured platforms" },
                                    { name: "get_analytics", desc: "Get engagement analytics" },
                                    { name: "delete_post", desc: "Delete a post" },
                                    { name: "health_check", desc: "Run health check" },
                                    { name: "get_logs", desc: "Retrieve log entries" }
                                ]

                                RowLayout {
                                    spacing: theme.spacingSm
                                    Rectangle {
                                        width: 4; height: 4; radius: 2
                                        color: theme.accent
                                    }
                                    Text {
                                        text: modelData.name
                                        font.pixelSize: 12
                                        font.family: "monospace"
                                        color: theme.textPrimary
                                    }
                                    Text {
                                        text: "— " + modelData.desc
                                        font.pixelSize: 11
                                        color: theme.textSecondary
                                    }
                                }
                            }
                        }

                        Text {
                            text: "Connect via stdio: xpst-mcp"
                            font.pixelSize: 11
                            font.family: "monospace"
                            color: theme.textMuted
                            visible: mcpRunning
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
                        Accessible.name: "Save Settings"
                        Accessible.role: Accessible.Button
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
                        Accessible.name: "Reset Settings"
                        Accessible.role: Accessible.Button
                    }
                }

                Item { Layout.fillWidth: true }
            }
        }
    }
}

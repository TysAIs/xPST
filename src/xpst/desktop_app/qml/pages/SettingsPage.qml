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
    property bool postCompletionAlerts: true
    property bool errorNotifications: true
    property bool mcpRunning: false
    property bool mcpReady: false
    property int mcpPid: 0
    property string mcpCommand: "xpst-mcp"
    property string mcpError: ""
    property var mcpTools: []

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
    Component.onCompleted: {
        loadFromConfig()
        refreshMcpStatus()
    }

    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onDataChanged() {
            loadFromConfig()
        }
        function onMcpStatusChanged() {
            refreshMcpStatus()
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
            if (cfg.video) {
                downloadDir = cfg.video.download_dir || ""
            }
            if (cfg.notifications) {
                postCompletionAlerts = cfg.notifications.enabled !== false && cfg.notifications.on_success !== false
                errorNotifications = cfg.notifications.enabled !== false && cfg.notifications.on_failure !== false
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

    function refreshMcpStatus() {
        if (typeof controller === "undefined") return
        try {
            var status = JSON.parse(controller.mcpStatus)
            mcpRunning = false
            mcpPid = status.pid || 0
            mcpCommand = status.command || "xpst-mcp"
            mcpError = status.error || ""
        } catch(e) {
            mcpRunning = false
            mcpError = "Could not read MCP status"
        }

        try {
            mcpTools = JSON.parse(controller.mcpTools)
        } catch(e2) {
            mcpTools = []
        }
    }

    function testMcpServer() {
        if (typeof controller === "undefined") return
        try {
            var raw = controller.testMcpServer()
            var result = JSON.parse(raw)
            refreshMcpStatus()
            mcpReady = result.ok === true
            if (result.ok) {
                showToast(result.message || "MCP command is ready", false)
            } else {
                mcpError = result.error || "MCP command check failed"
                showToast(mcpError, true)
            }
        } catch(e) {
            refreshMcpStatus()
            mcpReady = false
            showToast("MCP command check failed", true)
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
            video: {
                download_dir: downloadDir
            },
            notifications: {
                enabled: postCompletionAlerts || errorNotifications,
                on_success: postCompletionAlerts,
                on_failure: errorNotifications
            },
            monitoring: {
                log_level: "INFO"
            }
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
            anchors.margins: theme.pageMargin
            spacing: theme.spacingXl

            // Header
            Text {
                text: "Settings"
                font.pixelSize: 28
                font.weight: Font.DemiBold
                color: theme.textPrimary
            }

            // General Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "General"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
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
                        anchors.margins: theme.pageMargin
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
                    font.weight: Font.DemiBold
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
                        anchors.margins: theme.pageMargin
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
                                    indicator: Rectangle {
                                        implicitWidth: 44
                                        implicitHeight: 24
                                        x: parent.width - width
                                        y: parent.height / 2 - height / 2
                                        radius: height / 2
                                        color: parent.checked ? modelData.color : theme.surfaceAlt
                                        border.color: parent.checked ? modelData.color : theme.textMuted
                                        border.width: 1

                                        Rectangle {
                                            width: 18
                                            height: 18
                                            radius: 9
                                            x: parent.parent.checked ? parent.width - width - 3 : 3
                                            anchors.verticalCenter: parent.verticalCenter
                                            color: parent.parent.checked ? "#ffffff" : theme.textSecondary
                                        }
                                    }
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
                    font.weight: Font.DemiBold
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
                        anchors.margins: theme.pageMargin
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
                    font.weight: Font.DemiBold
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
                        anchors.margins: theme.pageMargin
                        spacing: theme.spacingMd

                        RowLayout {
                            Text {
                                text: "Post completion alerts"
                                font.pixelSize: 13
                                color: theme.textPrimary
                                Layout.fillWidth: true
                            }
                            Switch {
                                checked: settingsPage.postCompletionAlerts
                                onCheckedChanged: settingsPage.postCompletionAlerts = checked
                                indicator: Rectangle {
                                    implicitWidth: 44
                                    implicitHeight: 24
                                    x: parent.width - width
                                    y: parent.height / 2 - height / 2
                                    radius: height / 2
                                    color: parent.checked ? theme.accent : theme.surfaceAlt
                                    border.color: parent.checked ? theme.accent : theme.textMuted
                                    border.width: 1

                                    Rectangle {
                                        width: 18
                                        height: 18
                                        radius: 9
                                        x: parent.parent.checked ? parent.width - width - 3 : 3
                                        anchors.verticalCenter: parent.verticalCenter
                                        color: parent.parent.checked ? "#ffffff" : theme.textSecondary
                                    }
                                }
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
                                checked: settingsPage.errorNotifications
                                onCheckedChanged: settingsPage.errorNotifications = checked
                                indicator: Rectangle {
                                    implicitWidth: 44
                                    implicitHeight: 24
                                    x: parent.width - width
                                    y: parent.height / 2 - height / 2
                                    radius: height / 2
                                    color: parent.checked ? theme.accent : theme.surfaceAlt
                                    border.color: parent.checked ? theme.accent : theme.textMuted
                                    border.width: 1

                                    Rectangle {
                                        width: 18
                                        height: 18
                                        radius: 9
                                        x: parent.parent.checked ? parent.width - width - 3 : 3
                                        anchors.verticalCenter: parent.verticalCenter
                                        color: parent.parent.checked ? "#ffffff" : theme.textSecondary
                                    }
                                }
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
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                }

                Text {
                    text: "Current encoding parameters per platform"
                    font.pixelSize: 12
                    color: theme.textSecondary
                }

                // 3-column side-by-side layout for encoding comparison
                RowLayout {
                    Layout.fillWidth: true
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
                                maxDuration: "10m",
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
                                        font.weight: Font.DemiBold
                                        color: theme.textPrimary
                                    }
                                    Item { Layout.fillWidth: true }
                                }

                                GridLayout {
                                    columns: 2
                                    columnSpacing: theme.spacingMd
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

                                // Generate Sample button (#30)
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: theme.spacingSm

                                    Item { Layout.fillWidth: true }

                                    Rectangle {
                                        width: genSampleLabel.implicitWidth + 16
                                        height: 24
                                        radius: theme.radiusSm
                                        color: theme.accent
                                        opacity: genSampleMouse.containsMouse ? 0.85 : 1.0
                                        Text {
                                            id: genSampleLabel
                                            anchors.centerIn: parent
                                            text: "Generate sample"
                                            font.pixelSize: 9
                                            font.weight: Font.DemiBold
                                            color: "#ffffff"
                                        }
                                        MouseArea {
                                            id: genSampleMouse
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                // Build ffmpeg command for this platform's encoding
                                                var res = modelData.resolution.split(" ")[0].replace("×", "x")
                                                var fps = parseInt(modelData.fps) || 30
                                                var bitrate = modelData.bitrate.split("-")[0].trim().replace(" Mbps", "M")
                                                var outDir = settingsPage.downloadDir.length > 0 ? settingsPage.downloadDir : "/tmp"
                                                var outPath = outDir + "/xpst_sample_" + modelData.platform.toLowerCase() + ".mp4"
                                                // Use controller to generate sample if available
                                                if (typeof controller !== "undefined" && controller.generateEncodingSample) {
                                                    controller.generateEncodingSample(modelData.platform.toLowerCase(), outPath)
                                                }
                                                showToast("Generating sample for " + modelData.platform + "...", false)
                                            }
                                            Accessible.name: "Generate encoding sample for " + modelData.platform
                                            Accessible.role: Accessible.Button
                                        }
                                    }
                                }
                            }
                        }
                    }
                }


            }

            // Keyboard Shortcuts Section (#23)
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Keyboard Shortcuts"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: "Keyboard shortcuts section"
                    Accessible.role: Accessible.Heading
                }

                Text {
                    text: "Customize keyboard shortcuts. Click a shortcut field, then press your desired key combination."
                    font.pixelSize: 12
                    color: theme.textSecondary
                }

                property var shortcutDefs: [
                    { label: "Dashboard",    key: "Ctrl+1", defaultKey: "Ctrl+1" },
                    { label: "Content",      key: "Ctrl+2", defaultKey: "Ctrl+2" },
                    { label: "Analytics",    key: "Ctrl+3", defaultKey: "Ctrl+3" },
                    { label: "Connect",      key: "Ctrl+4", defaultKey: "Ctrl+4" },
                    { label: "Schedule",     key: "Ctrl+5", defaultKey: "Ctrl+5" },
                    { label: "Refresh",      key: "Ctrl+R", defaultKey: "Ctrl+R" },
                    { label: "Quit",         key: "Ctrl+Q", defaultKey: "Ctrl+Q" }
                ]

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: shortcutListCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: shortcutListCol
                        anchors.fill: parent
                        anchors.margins: theme.pageMargin
                        spacing: 0

                        Repeater {
                            model: parent.parent.parent.shortcutDefs

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 44
                                color: "transparent"

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: theme.spacingMd
                                    anchors.rightMargin: theme.spacingMd
                                    spacing: theme.spacingMd

                                    Text {
                                        text: modelData.label
                                        font.pixelSize: 13
                                        color: theme.textPrimary
                                        Layout.preferredWidth: 120
                                    }

                                    Rectangle {
                                        Layout.preferredWidth: 160
                                        Layout.preferredHeight: 32
                                        radius: theme.radiusSm
                                        color: theme.surfaceAlt
                                        border.color: shortcutField.activeFocus ? theme.accent : "transparent"
                                        border.width: shortcutField.activeFocus ? 2 : 0

                                        TextInput {
                                            id: shortcutField
                                            anchors.fill: parent
                                            anchors.margins: theme.spacingSm
                                            verticalAlignment: TextInput.AlignVCenter
                                            font.pixelSize: 12
                                            font.family: theme.monoFontFamily
                                            color: theme.textPrimary
                                            readOnly: true
                                            text: modelData.key

                                            Keys.onPressed: function(event) {
                                                var mods = []
                                                if (event.modifiers & Qt.ControlModifier) mods.push("Ctrl")
                                                if (event.modifiers & Qt.AltModifier) mods.push("Alt")
                                                if (event.modifiers & Qt.ShiftModifier) mods.push("Shift")
                                                if (event.modifiers & Qt.MetaModifier) mods.push("Meta")

                                                var keyName = ""
                                                if (event.key >= Qt.Key_A && event.key <= Qt.Key_Z) {
                                                    keyName = String.fromCharCode(event.key)
                                                } else if (event.key >= Qt.Key_0 && event.key <= Qt.Key_9) {
                                                    keyName = String.fromCharCode(event.key)
                                                } else if (event.key === Qt.Key_F1) { keyName = "F1"
                                                } else if (event.key === Qt.Key_F2) { keyName = "F2"
                                                } else if (event.key === Qt.Key_F3) { keyName = "F3"
                                                } else if (event.key === Qt.Key_F4) { keyName = "F4"
                                                } else if (event.key === Qt.Key_F5) { keyName = "F5"
                                                } else if (event.key === Qt.Key_F6) { keyName = "F6"
                                                } else if (event.key === Qt.Key_F7) { keyName = "F7"
                                                } else if (event.key === Qt.Key_F8) { keyName = "F8"
                                                } else if (event.key === Qt.Key_F9) { keyName = "F9"
                                                } else if (event.key === Qt.Key_F10) { keyName = "F10"
                                                } else if (event.key === Qt.Key_F11) { keyName = "F11"
                                                } else if (event.key === Qt.Key_F12) { keyName = "F12"
                                                } else if (event.key === Qt.Key_Escape) { keyName = "Esc"
                                                } else if (event.key === Qt.Key_Space) { keyName = "Space"
                                                } else if (event.key === Qt.Key_Tab) { keyName = "Tab"
                                                } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { keyName = "Enter"
                                                } else if (event.key === Qt.Key_Delete) { keyName = "Del"
                                                } else if (event.key === Qt.Key_Backspace) { keyName = "Backspace"
                                                } else { keyName = "" }

                                                if (keyName) {
                                                    if (mods.length > 0) {
                                                        shortcutField.text = mods.join("+") + "+" + keyName
                                                    } else {
                                                        shortcutField.text = keyName
                                                    }
                                                    // Update the model
                                                    var defs = shortcutDefs.slice()
                                                    defs[index] = { label: modelData.label, key: shortcutField.text, defaultKey: modelData.defaultKey }
                                                    shortcutDefs = defs
                                                }
                                                event.accepted = true
                                            }
                                        }
                                    }

                                    Rectangle {
                                        Layout.preferredWidth: 56
                                        Layout.preferredHeight: 26
                                        radius: theme.radiusSm
                                        color: resetMouse.containsMouse ? theme.surfaceAlt : "transparent"
                                        border.color: theme.surfaceAlt
                                        border.width: 1

                                        Text {
                                            anchors.centerIn: parent
                                            text: "Reset"
                                            font.pixelSize: 10
                                            color: theme.textSecondary
                                        }

                                        MouseArea {
                                            id: resetMouse
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                var defs = shortcutDefs.slice()
                                                defs[index] = { label: modelData.label, key: modelData.defaultKey, defaultKey: modelData.defaultKey }
                                                shortcutDefs = defs
                                            }
                                        }
                                    }

                                    Item { Layout.fillWidth: true }
                                }

                                Rectangle {
                                    anchors.bottom: parent.bottom
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.leftMargin: theme.spacingMd
                                    anchors.rightMargin: theme.spacingMd
                                    height: 1
                                    color: theme.surfaceAlt
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
                    font.weight: Font.DemiBold
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
                        anchors.margins: theme.pageMargin
                        spacing: theme.spacingMd

                        Text {
                            text: "MCP Server"
                            font.pixelSize: 14
                            font.weight: Font.DemiBold
                            color: theme.textPrimary
                        }

                        RowLayout {
                            spacing: theme.spacingMd
                            Rectangle {
                                width: 8; height: 8; radius: 4
                                color: mcpReady ? theme.success : (mcpError.length > 0 ? theme.error : theme.warning)
                            }
                            Text {
                                text: mcpReady ? "Ready" : (mcpError.length > 0 ? "Needs attention" : "Not tested")
                                font.pixelSize: 13
                                color: theme.textSecondary
                            }
                            Item { Layout.fillWidth: true }

                            Rectangle {
                                width: mcpBtnLabel.implicitWidth + theme.spacingXxl
                                height: 32
                                radius: theme.radiusMd
                                color: theme.accent
                                opacity: mcpBtnMouse.containsMouse ? 0.85 : 1.0

                                Text {
                                    id: mcpBtnLabel
                                    anchors.centerIn: parent
                                    text: "Test"
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    color: "#ffffff"
                                }

                                MouseArea {
                                    id: mcpBtnMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: settingsPage.testMcpServer()
                                    Accessible.name: "Test MCP command"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }

                        // MCP Tools list
                        Text {
                            text: "Available Tools"
                            font.pixelSize: 12
                            color: theme.textMuted
                            visible: mcpReady
                        }

                        ColumnLayout {
                            spacing: theme.spacingXs
                            visible: mcpReady

                            Repeater {
                                model: settingsPage.mcpTools

                                RowLayout {
                                    spacing: theme.spacingSm
                                    Rectangle {
                                        width: 4; height: 4; radius: 2
                                        color: theme.accent
                                    }
                                    Text {
                                        text: modelData.name
                                        font.pixelSize: 12
                                        font.family: theme.monoFontFamily
                                        color: theme.textPrimary
                                    }
                                    Text {
                                        text: "- " + (modelData.description || "")
                                        font.pixelSize: 11
                                        color: theme.textSecondary
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                    }
                                }
                            }
                        }

                        Text {
                            text: "Connect via stdio: " + mcpCommand
                            font.pixelSize: 11
                            font.family: theme.monoFontFamily
                            color: theme.textMuted
                        }

                        Text {
                            text: mcpError
                            font.pixelSize: 11
                            color: theme.error
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                            visible: mcpError.length > 0 && !mcpReady
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
                        font.weight: Font.DemiBold
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

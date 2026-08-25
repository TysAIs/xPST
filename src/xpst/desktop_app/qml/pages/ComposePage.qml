import xpst.desktop_app.qml 1.0
import QtQuick 2.15
import xpst.desktop_app.qml 1.0
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import "../components"


Page {
    id: composePage
    background: Rectangle { color: theme.canvas }

    // ── State ─────────────────────────────────────────────────────
    property string selectedVideoPath: ""
    property string selectedVideoName: ""
    property string selectedThumbnail: ""
    property string captionText: ""
    property var selectedPlatforms: ({"youtube": true, "instagram": false, "x": false, "tiktok": false})
    property var localVideos: []
    property string scannedFolder: ""
    property bool loadingVideos: false
    property bool posting: false
    property var uploadProgress: ({})
    property string lastResult: ""
    property bool lastResultError: false
    property var preflight: ({ ready: false, blocking: [], warnings: [], platforms: [] })

    property int maxCaptionLength: 2200

    function closeDialog() {}

    Component.onCompleted: {
        // Auto-load default folder videos if controller is available
        if (typeof controller !== "undefined" && controller.getLocalVideos) {
            loadDefaultVideos()
        }
    }

    function loadDefaultVideos() {
        composePage.loadingVideos = true
        try {
            var raw = controller.getLocalVideos("")
            var parsed = JSON.parse(raw)
            if (parsed.ok) {
                composePage.localVideos = parsed.videos || []
                composePage.scannedFolder = parsed.folder || ""
            } else {
                composePage.localVideos = []
                console.warn("getLocalVideos returned not-ok:", parsed.error || "")
            }
        } catch(e) {
            console.error("ComposePage loadDefaultVideos failed", e)
            composePage.localVideos = []
            if (typeof showToast !== "undefined") showToast("Could not load default videos", true)
        }
        composePage.loadingVideos = false
    }

    function browseForFolder() {
        if (typeof controller === "undefined" || !controller.browseForFolder) return
        try {
            var folder = controller.browseForFolder()
            if (folder && folder.length > 0) {
                scanFolder(folder)
            }
        } catch(e) {
            console.error("ComposePage browseForFolder failed", e)
            if (typeof showToast !== "undefined") showToast("Could not open folder browser", true)
        }
    }

    function scanFolder(folderPath) {
        composePage.loadingVideos = true
        try {
            var raw = controller.getLocalVideos(folderPath)
            var parsed = JSON.parse(raw)
            if (parsed.ok) {
                composePage.localVideos = parsed.videos || []
                composePage.scannedFolder = parsed.folder || ""
            } else {
                composePage.localVideos = []
                if (typeof showToast !== "undefined")
                    showToast(parsed.error || "Failed to scan folder", true)
                else
                    console.warn("scanFolder not ok:", parsed.error || "")
            }
        } catch(e) {
            console.error("ComposePage scanFolder failed", e)
            composePage.localVideos = []
        }
        composePage.loadingVideos = false
    }

    function selectVideo(path, name, thumbnail) {
        composePage.selectedVideoPath = path
        composePage.selectedVideoName = name
        composePage.selectedThumbnail = thumbnail || ""
        if (!thumbnail && typeof controller !== "undefined" && controller.getThumbnail) {
            try {
                composePage.selectedThumbnail = controller.getThumbnail(path)
            } catch(e) {
                console.error("ComposePage getThumbnail failed", e)
            }
        }
        refreshPreflight()
    }

    function togglePlatform(name) {
        var p = Object.assign({}, composePage.selectedPlatforms)
        p[name] = !p[name]
        composePage.selectedPlatforms = p
        refreshPreflight()
    }

    function selectedPlatformList() {
        var result = []
        var p = composePage.selectedPlatforms
        for (var k in p) {
            if (p[k]) result.push(k)
        }
        return result
    }

    function isPlatformSelected(name) {
        return composePage.selectedPlatforms[name] === true
    }

    function selectedCount() {
        var c = 0
        var p = composePage.selectedPlatforms
        for (var k in p) {
            if (p[k]) c++
        }
        return c
    }

    function refreshPreflight() {
        if (typeof controller === "undefined" || !controller.previewPost) {
            composePage.preflight = { ready: false, blocking: ["Preview unavailable"], warnings: [], platforms: [] }
            return
        }
        var platformsJson = JSON.stringify(selectedPlatformList())
        try {
            var raw = controller.previewPost(composePage.selectedVideoPath, composePage.captionText, platformsJson)
            var parsed = JSON.parse(raw)
            if (parsed.ok) {
                composePage.preflight = parsed
            } else {
                composePage.preflight = { ready: false, blocking: [parsed.error || "Preview failed"], warnings: [], platforms: [] }
            }
        } catch(e) {
            console.error("ComposePage refreshPreflight failed", e)
            composePage.preflight = { ready: false, blocking: ["Preview failed"], warnings: [], platforms: [] }
        }
    }

    function postNow() {
        if (composePage.selectedVideoPath.length === 0) {
            if (typeof showToast !== "undefined") showToast("Select a video first", true)
            return
        }
        if (selectedCount() === 0) {
            if (typeof showToast !== "undefined") showToast("Select at least one platform", true)
            return
        }
        if (typeof controller === "undefined" || !controller.postVideo) return

        composePage.posting = true
        composePage.uploadProgress = {}
        var platformsJson = JSON.stringify(selectedPlatformList())

        try {
            controller.postVideo(composePage.selectedVideoPath, composePage.captionText, platformsJson)
            if (typeof showToast !== "undefined")
                showToast("Posting to " + selectedCount() + " platform(s)...", false)
        } catch(e) {
            console.error("ComposePage postNow failed", e)
            composePage.posting = false
            if (typeof showToast !== "undefined")
                showToast("Failed to start post: " + e, true)
        }
    }

    function platformColor(name) {
        var n = (name || "").toLowerCase()
        if (n === "youtube") return theme.youtube
        if (n === "instagram") return theme.instagram
        if (n === "x") return theme.xtwitter
        if (n === "tiktok") return theme.tiktok
        return theme.accent
    }

    function platformIcon(name) {
        var n = (name || "").toLowerCase()
        if (n === "youtube") return Icons.youtube
        if (n === "instagram") return Icons.instagram
        if (n === "x") return Icons.x
        if (n === "tiktok") return Icons.tiktok
        return Icons.video
    }

    function platformLabel(name) {
        var n = (name || "").toLowerCase()
        if (n === "youtube") return "YouTube"
        if (n === "instagram") return "Instagram"
        if (n === "x") return "X"
        if (n === "tiktok") return "TikTok"
        return name
    }

    function formatFileSize(bytes) {
        if (!bytes || bytes <= 0) return ""
        if (bytes < 1024) return bytes + " B"
        if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB"
        return (bytes / (1024 * 1024)).toFixed(1) + " MB"
    }

    // ── Signal connections ────────────────────────────────────────
    Connections {
        target: typeof controller !== "undefined" ? controller : null

        function onProgressChanged(platform, pct) {
            var p = Object.assign({}, composePage.uploadProgress)
            p[platform] = pct
            composePage.uploadProgress = p
        }

        function onPostComplete(resultJson) {
            composePage.posting = false
            try {
                var result = JSON.parse(resultJson)
                composePage.lastResult = result
                composePage.lastResultError = !result.all_success

                var successCount = 0
                var failCount = 0
                var platforms = result.platforms || {}
                for (var k in platforms) {
                    if (platforms[k].success) successCount++
                    else failCount++
                }

                if (result.all_success) {
                    if (typeof showToast !== "undefined")
                        showToast("Posted successfully to all platforms!", false)
                } else if (successCount > 0) {
                    if (typeof showToast !== "undefined")
                        showToast("Posted to " + successCount + " platform(s), " + failCount + " failed", true)
                } else {
                    if (typeof showToast !== "undefined")
                        showToast("Post failed on all platforms", true)
                }
            } catch(e) {
                console.error("ComposePage onPostComplete parse failed", e)
                composePage.lastResultError = true
                if (typeof showToast !== "undefined")
                    showToast("Post completed (result parse error)", true)
            }
        }

        function onError(msg) {
            composePage.posting = false
            composePage.lastResultError = true
        }
    }

    // ── Layout ────────────────────────────────────────────────────
    Flickable {
        anchors.fill: parent
        contentHeight: contentCol.implicitHeight + theme.spacingXxl
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: contentCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: theme.pageMargin
            spacing: theme.spacingXl

            // Header
            ColumnLayout {
                spacing: theme.spacingXs
                Text {
                    text: "Compose"
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: "Compose page title"
                    Accessible.role: Accessible.Heading
                }
                Text {
                    text: "Create and cross-post a video from scratch"
                    font.pixelSize: 13
                    color: theme.textSecondary
                }
            }

            // ── Two-column layout: left = video picker + caption, right = preview + platforms ──
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingXl

                // ── Left column: video picker + caption ──
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 2
                    spacing: theme.spacingLg

                    // Video picker section
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingMd

                            Text {
                                text: "Select Video"
                                font.pixelSize: 16
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                                Layout.fillWidth: true
                            }

                            // Browse button
                            Rectangle {
                                width: browseLabel.implicitWidth + 28
                                height: theme.controlHeightMd
                                radius: theme.radiusMd
                                color: browseMouse.containsMouse ? theme.accentHover : theme.accent

                                Text {
                                    id: browseLabel
                                    anchors.centerIn: parent
                                    text: "Browse Folder"
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    color: "#ffffff"
                                }

                                MouseArea {
                                    id: browseMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: composePage.browseForFolder()
                                    Accessible.name: "Browse for video folder"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }

                        // Scanned folder path
                        Text {
                            text: composePage.scannedFolder.length > 0
                                  ? "Folder: " + composePage.scannedFolder
                                  : "No folder selected"
                            font.pixelSize: 11
                            color: theme.textMuted
                            Layout.fillWidth: true
                            elide: Text.ElideMiddle
                            visible: composePage.scannedFolder.length > 0
                        }

                        // Video grid
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.max(160, videoGridCol.implicitHeight + theme.spacingLg)
                            radius: theme.radiusLg
                            color: theme.surfaceCard
                            clip: true

                            ColumnLayout {
                                id: videoGridCol
                                anchors.fill: parent
                                anchors.margins: theme.spacingMd
                                spacing: theme.spacingSm

                                // Loading state (Phase 3.1: shimmer skeleton)
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: theme.spacingSm
                                    visible: composePage.loadingVideos
                                    Layout.alignment: Qt.AlignHCenter

                                    LoadingSkeleton {
                                        Layout.preferredWidth: 160
                                        Layout.preferredHeight: 16
                                        running: composePage.loadingVideos
                                        Layout.alignment: Qt.AlignHCenter
                                    }

                                    Text {
                                        text: "Scanning for videos..."
                                        font.pixelSize: 12
                                        color: theme.textMuted
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                }

                                // Empty state
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: theme.spacingSm
                                    visible: !composePage.loadingVideos && composePage.localVideos.length === 0
                                    Layout.alignment: Qt.AlignHCenter

                                    Text {
                                        text: Icons.video
                                        font.family: theme.iconFontFamily
                                        font.pixelSize: 36
                                        color: theme.textMuted
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                    Text {
                                        text: "No videos found"
                                        font.pixelSize: 13
                                        color: theme.textMuted
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                    Text {
                                        text: "Browse for a folder containing video files"
                                        font.pixelSize: 11
                                        color: theme.textMuted
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                }

                                // Video grid
                                GridLayout {
                                    Layout.fillWidth: true
                                    columns: Math.max(2, Math.floor(parent.width / 180))
                                    columnSpacing: theme.spacingMd
                                    rowSpacing: theme.spacingMd
                                    visible: !composePage.loadingVideos && composePage.localVideos.length > 0

                                    Repeater {
                                        model: composePage.localVideos

                                        Rectangle {
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 130
                                            radius: theme.radiusMd
                                            color: composePage.selectedVideoPath === modelData.path
                                                   ? theme.accentMuted
                                                   : videoTileMouse.containsMouse ? theme.accentHover
                                                   : theme.surfaceAlt
                                            Behavior on color { ColorAnimation { duration: 120 } }
                                            border.color: composePage.selectedVideoPath === modelData.path
                                                          ? theme.accent : "transparent"
                                            border.width: 2
                                            clip: true

                                            ColumnLayout {
                                                anchors.fill: parent
                                                spacing: 0

                                                // Thumbnail
                                                Rectangle {
                                                    Layout.fillWidth: true
                                                    Layout.preferredHeight: 80
                                                    color: "#000000"
                                                    clip: true

                                                    Image {
                                                        anchors.fill: parent
                                                        fillMode: Image.PreserveAspectCrop
                                                        source: modelData.thumbnail || ""
                                                        visible: status === Image.Ready
                                                        asynchronous: true
                                                    }

                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: Icons.video
                                                        font.family: theme.iconFontFamily
                                                        font.pixelSize: 24
                                                        color: theme.textMuted
                                                        visible: !thumbImg.visible || thumbImg.status !== Image.Ready
                                                    }

                                                    Image {
                                                        id: thumbImg
                                                        anchors.fill: parent
                                                        fillMode: Image.PreserveAspectCrop
                                                        source: modelData.thumbnail || ""
                                                        visible: false
                                                        asynchronous: true
                                                    }

                                                    // Selected checkmark
                                                    Rectangle {
                                                        anchors.top: parent.top
                                                        anchors.right: parent.right
                                                        anchors.margins: 4
                                                        width: 20; height: 20; radius: 10
                                                        color: theme.accent
                                                        visible: composePage.selectedVideoPath === modelData.path

                                                        Text {
                                                            anchors.centerIn: parent
                                                            text: Icons.check
                                                            font.family: theme.iconFontFamily
                                                            font.pixelSize: 11
                                                            color: "#ffffff"
                                                        }
                                                    }
                                                }

                                                // Name
                                                Text {
                                                    Layout.fillWidth: true
                                                    Layout.leftMargin: theme.spacingSm
                                                    Layout.rightMargin: theme.spacingSm
                                                    Layout.topMargin: theme.spacingXs
                                                    text: modelData.name || ""
                                                    font.pixelSize: 11
                                                    color: theme.textPrimary
                                                    elide: Text.ElideRight
                                                    maximumLineCount: 1
                                                }

                                                Text {
                                                    Layout.fillWidth: true
                                                    Layout.leftMargin: theme.spacingSm
                                                    Layout.rightMargin: theme.spacingSm
                                                    text: composePage.formatFileSize(modelData.size || 0)
                                                    font.pixelSize: 10
                                                    color: theme.textMuted
                                                }
                                            }

                                            MouseArea {
                                                id: videoTileMouse
                                                anchors.fill: parent
                                                hoverEnabled: true
                                                cursorShape: Qt.PointingHandCursor
                                                onClicked: composePage.selectVideo(modelData.path, modelData.name, modelData.thumbnail)
                                                Accessible.name: "Select video " + (modelData.name || "")
                                                Accessible.role: Accessible.Button
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Caption editor
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingSm

                            Text {
                                text: "Caption"
                                font.pixelSize: 16
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                                Layout.fillWidth: true
                            }

                            Text {
                                text: {
                                    var len = captionInput.length
                                    var limits = { "x": 280, "threads": 500, "instagram": 2200, "youtube": 5000, "tiktok": 2200 }
                                    var parts = []
                                    var sp = composePage.selectedPlatforms || ({})
                                    var activeCount = 0
                                    for (var key in sp) {
                                        if (sp[key]) activeCount++
                                    }
                                    if (activeCount === 0) {
                                        parts.push(len + " / " + composePage.maxCaptionLength)
                                    } else {
                                        for (var p in sp) {
                                            if (!sp[p]) continue
                                            var limit = limits[p] || composePage.maxCaptionLength
                                            parts.push(p.charAt(0).toUpperCase() + p.slice(1) + ": " + len + "/" + limit)
                                        }
                                    }
                                    return parts.join("  ·  ")
                                }
                                font.pixelSize: 11
                                color: {
                                    var limits = { "x": 280, "threads": 500, "instagram": 2200, "youtube": 5000, "tiktok": 2200 }
                                    var sp = composePage.selectedPlatforms || ({})
                                    for (var p in sp) {
                                        if (!sp[p]) continue
                                        var limit = limits[p] || composePage.maxCaptionLength
                                        if (captionInput.length > limit) return theme.error
                                    }
                                    return theme.textMuted
                                }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 120
                            radius: theme.radiusMd
                            color: theme.surfaceCard
                            border.color: theme.surfaceAlt
                            border.width: 1

                            Flickable {
                                anchors.fill: parent
                                anchors.margins: theme.spacingSm
                                clip: true
                                contentHeight: captionInput.implicitHeight
                                flickableDirection: Flickable.VerticalFlick

                                TextArea {
                                    id: captionInput
                                    width: parent.width
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    wrapMode: TextArea.Wrap
                                    placeholderText: "Write your caption..."
                                    placeholderTextColor: theme.textMuted
                                    background: Rectangle { color: "transparent" }
                                    onTextChanged: {
                                        composePage.captionText = text
                                        refreshPreflightTimer.restart()
                                    }

                                    Accessible.name: "Caption editor"
                                    Accessible.role: Accessible.EditableText
                                }
                            }
                        }
                    }
                }

                // ── Right column: preview + platform selection + post button ──
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    spacing: theme.spacingLg

                    // Preview area
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm

                        Text {
                            text: "Preview"
                            font.pixelSize: 16
                            font.weight: Font.DemiBold
                            color: theme.textPrimary
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 200
                            radius: theme.radiusLg
                            color: "#000000"
                            clip: true

                            Image {
                                id: previewImage
                                anchors.fill: parent
                                fillMode: Image.PreserveAspectCrop
                                source: composePage.selectedThumbnail
                                visible: status === Image.Ready && composePage.selectedThumbnail.length > 0
                                asynchronous: true
                            }

                            // Empty preview state
                            ColumnLayout {
                                anchors.centerIn: parent
                                spacing: theme.spacingSm
                                visible: !previewImage.visible || previewImage.status !== Image.Ready

                                Text {
                                    text: Icons.video
                                    font.family: theme.iconFontFamily
                                    font.pixelSize: 40
                                    color: theme.textMuted
                                    Layout.alignment: Qt.AlignHCenter
                                }
                                Text {
                                    text: "No video selected"
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                    Layout.alignment: Qt.AlignHCenter
                                }
                            }
                        }

                        // Selected video name
                        Text {
                            Layout.fillWidth: true
                            text: composePage.selectedVideoName.length > 0
                                  ? composePage.selectedVideoName : "Select a video from the grid"
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                            color: composePage.selectedVideoName.length > 0
                                   ? theme.textPrimary : theme.textMuted
                            elide: Text.ElideMiddle
                            visible: composePage.selectedVideoName.length > 0
                        }
                    }

                    // Platform selection
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm

                        Text {
                            text: "Destinations"
                            font.pixelSize: 16
                            font.weight: Font.DemiBold
                            color: theme.textPrimary
                        }

                        Repeater {
                            model: [
                                { name: "youtube", label: "YouTube", icon: Icons.youtube },
                                { name: "instagram", label: "Instagram", icon: Icons.instagram },
                                { name: "x", label: "X", icon: Icons.x },
                                { name: "tiktok", label: "TikTok", icon: Icons.tiktok }
                            ]

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 44
                                radius: theme.radiusMd
                                color: composePage.isPlatformSelected(modelData.name)
                                       ? theme.accentMuted
                                       : platformToggleMouse.containsMouse ? theme.accentHover
                                       : theme.surfaceCard
                                Behavior on color { ColorAnimation { duration: 120 } }
                                border.color: composePage.isPlatformSelected(modelData.name)
                                              ? composePage.platformColor(modelData.name) : theme.surfaceAlt
                                border.width: composePage.isPlatformSelected(modelData.name) ? 2 : 1

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: theme.spacingMd
                                    anchors.rightMargin: theme.spacingMd
                                    spacing: theme.spacingMd

                                    // Checkbox indicator
                                    Rectangle {
                                        width: 18; height: 18; radius: 4
                                        color: composePage.isPlatformSelected(modelData.name)
                                               ? composePage.platformColor(modelData.name) : "transparent"
                                        border.color: composePage.isPlatformSelected(modelData.name)
                                                      ? composePage.platformColor(modelData.name) : theme.textMuted
                                        border.width: 2

                                        Text {
                                            anchors.centerIn: parent
                                            text: Icons.check
                                            font.family: theme.iconFontFamily
                                            font.pixelSize: 11
                                            color: "#ffffff"
                                            visible: composePage.isPlatformSelected(modelData.name)
                                        }
                                    }

                                    // Platform icon
                                    Text {
                                        text: modelData.icon
                                        font.family: theme.iconFontFamily
                                        font.pixelSize: 14
                                        color: composePage.platformColor(modelData.name)
                                        Layout.preferredWidth: 20
                                        horizontalAlignment: Text.AlignHCenter
                                    }

                                    Text {
                                        text: modelData.label
                                        font.pixelSize: 13
                                        font.weight: composePage.isPlatformSelected(modelData.name)
                                                    ? Font.DemiBold : Font.Normal
                                        color: composePage.isPlatformSelected(modelData.name)
                                               ? theme.textPrimary : theme.textSecondary
                                        Layout.fillWidth: true
                                    }

                                    Text {
                                        text: composePage.selectedCount()
                                        font.pixelSize: 10
                                        color: theme.textMuted
                                        visible: false
                                    }
                                }

                                MouseArea {
                                    id: platformToggleMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: composePage.togglePlatform(modelData.name)
                                    Accessible.name: "Toggle " + modelData.label + " destination"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }
                    }

                    // Preflight warnings/blockers
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingXs
                        visible: composePage.selectedVideoPath.length > 0

                        Repeater {
                            model: (composePage.preflight.blocking || []).concat(composePage.preflight.warnings || [])

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: theme.spacingSm

                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    color: index < (composePage.preflight.blocking || []).length
                                           ? theme.error : theme.warning
                                }

                                Text {
                                    text: modelData
                                    font.pixelSize: 11
                                    color: theme.textSecondary
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }

                    // Upload progress
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm
                        visible: composePage.posting || Object.keys(composePage.uploadProgress).length > 0

                        Text {
                            text: "Upload Progress"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            color: theme.textPrimary
                            visible: composePage.posting
                        }

                        Repeater {
                            model: Object.keys(composePage.uploadProgress)

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: theme.spacingSm

                                Text {
                                    text: composePage.platformIcon(modelData)
                                    font.family: theme.iconFontFamily
                                    font.pixelSize: 12
                                    color: composePage.platformColor(modelData)
                                    Layout.preferredWidth: 18
                                }

                                Text {
                                    text: composePage.platformLabel(modelData)
                                    font.pixelSize: 11
                                    color: theme.textSecondary
                                    Layout.preferredWidth: 70
                                }

                                ProgressBar {
                                    from: 0
                                    to: 100
                                    value: composePage.uploadProgress[modelData] || 0
                                    Layout.fillWidth: true

                                    background: Rectangle {
                                        implicitHeight: 6
                                        color: theme.surfaceAlt
                                        radius: 3
                                    }

                                    contentItem: Item {
                                        implicitHeight: 6
                                        Rectangle {
                                            width: parent.width * parent.parent.value / 100
                                            height: 6
                                            radius: 3
                                            color: composePage.platformColor(modelData)
                                        }
                                    }
                                }

                                Text {
                                    text: Math.round(composePage.uploadProgress[modelData] || 0) + "%"
                                    font.pixelSize: 11
                                    color: theme.textMuted
                                    Layout.preferredWidth: 36
                                }
                            }
                        }
                    }

                    // Result message
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: resultCol.implicitHeight + theme.spacingMd
                        radius: theme.radiusMd
                        color: composePage.lastResultError ? Qt.rgba(1, 0.27, 0.23, 0.12) : Qt.rgba(0.19, 0.82, 0.35, 0.10)
                        visible: composePage.lastResult !== "" && !composePage.posting

                        ColumnLayout {
                            id: resultCol
                            anchors.fill: parent
                            anchors.margins: theme.spacingSm
                            spacing: theme.spacingXs

                            RowLayout {
                                spacing: theme.spacingSm

                                Text {
                                    text: composePage.lastResultError ? Icons.error : Icons.check
                                    font.family: theme.iconFontFamily
                                    font.pixelSize: 14
                                    color: composePage.lastResultError ? theme.error : theme.success
                                }

                                Text {
                                    text: composePage.lastResultError ? "Post had errors" : "Post successful!"
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    color: composePage.lastResultError ? theme.error : theme.success
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }

                    // Post button
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: theme.controlHeightLg
                        radius: theme.radiusMd
                        color: {
                            if (composePage.posting) return theme.surfaceAlt
                            if (composePage.selectedVideoPath.length === 0) return theme.surfaceAlt
                            if (composePage.selectedCount() === 0) return theme.surfaceAlt
                            return postMouse.containsMouse ? theme.accentHover : theme.accent
                        }

                        RowLayout {
                            anchors.centerIn: parent
                            spacing: theme.spacingSm

                            Text {
                                text: composePage.posting ? "" : Icons.plus
                                font.family: theme.iconFontFamily
                                font.pixelSize: 14
                                color: "#ffffff"
                                visible: !composePage.posting
                            }

                            Text {
                                text: composePage.posting ? "Posting..." : "Post Now"
                                font.pixelSize: 14
                                font.weight: Font.DemiBold
                                color: (composePage.posting || composePage.selectedVideoPath.length === 0
                                        || composePage.selectedCount() === 0)
                                       ? theme.textMuted : "#ffffff"
                            }
                        }

                        MouseArea {
                            id: postMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: (composePage.posting || composePage.selectedVideoPath.length === 0
                                          || composePage.selectedCount() === 0)
                                         ? Qt.ArrowCursor : Qt.PointingHandCursor
                            enabled: !composePage.posting && composePage.selectedVideoPath.length > 0
                                     && composePage.selectedCount() > 0
                            onClicked: composePage.postNow()
                            Accessible.name: "Post video now"
                            Accessible.role: Accessible.Button
                        }
                    }
                }
            }
        }
    }

    // Debounce preflight refresh while typing caption
    Timer {
        id: refreshPreflightTimer
        interval: 400
        repeat: false
        onTriggered: composePage.refreshPreflight()
    }
}

import xpst.desktop_app.qml 1.0
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtMultimedia
import "../components"


Page {
    id: detailPage
    background: Rectangle { color: theme.canvas }

    // Properties set by the parent when navigating to this page
    property string postId: ""
    property string videoPath: ""
    property string caption: ""
    property string sourcePlatform: ""
    property var postData: ({})
    property string activeTab: "youtube"
    property bool loading: false
    property bool deleting: false

    // ── Load post data when postId changes ───────────────────────
    onPostIdChanged: loadPostData()

    function loadPostData() {
        if (!postId) return
        loading = true
        try {
            if (typeof controller !== "undefined" && controller.recentPosts) {
                var posts = JSON.parse(controller.recentPosts)
                for (var i = 0; i < posts.length; i++) {
                    if (posts[i].title === postId || posts[i].postId === postId) {
                        postData = posts[i]
                        caption = posts[i].caption || posts[i].title || ""
                        sourcePlatform = posts[i].source_platform || ""
                        videoPath = posts[i].video_path || posts[i].local_path || ""
                        break
                    }
                }
            }
        } catch(e) {
            console.error("DetailPanel loadPostData failed", e)
        }
        loading = false
    }

    // ── Platform tabs ────────────────────────────────────────────
    function platformList() {
        var platforms = []
        if (postData && postData.platforms) {
            for (var key in postData.platforms) {
                platforms.push(key)
            }
        }
        if (platforms.length === 0) {
            platforms = ["youtube", "instagram", "x", "tiktok", "threads", "linkedin"]
        }
        return platforms
    }

    function platformIcon(name) {
        if (name === "youtube") return theme.iconYouTube || Icons.play
        if (name === "instagram") return theme.iconInstagram || Icons.camera
        if (name === "x") return theme.iconX || Icons.share
        if (name === "tiktok") return Icons.play
        if (name === "threads") return "T"
        if (name === "linkedin") return "in"
        return Icons.content
    }

    function platformDisplayName(name) {
        var names = {
            "youtube": "YouTube",
            "instagram": "Instagram",
            "x": "X",
            "tiktok": "TikTok",
            "threads": "Threads",
            "linkedin": "LinkedIn"
        }
        return names[name] || name
    }

    function platformData(name) {
        if (postData && postData.platforms && postData.platforms[name])
            return postData.platforms[name]
        return {}
    }

    function deletePost(platform) {
        if (!postId || !platform) return
        deleting = true
        if (typeof controller !== "undefined") {
            controller.deletePost(postId, platform)
        }
    }

    // Listen for delete completion
    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onPostComplete(resultJson) {
            deleting = false
            try {
                var result = JSON.parse(resultJson)
                if (result.success) {
                    // Refresh data
                    loadPostData()
                }
            } catch(e) {
                console.error("DetailPanel deleteComplete parse failed", e)
            }
        }
    }

    // ── Loading state (Phase 3.1: shimmer skeleton) ─────────────
    ColumnLayout {
        anchors.centerIn: parent
        visible: loading
        spacing: theme.spacingMd
        width: 260

        Repeater {
            model: 3
            LoadingSkeleton {
                Layout.fillWidth: true
                Layout.preferredHeight: 56
                running: loading
            }
        }
    }

    // ── Empty state ──────────────────────────────────────────────
    ColumnLayout {
        anchors.centerIn: parent
        visible: !loading && !postId
        spacing: theme.spacingMd

        Text {
            Layout.alignment: Qt.AlignHCenter
            text: Icons.content
            font.family: theme.iconFontFamily
            font.pixelSize: 48
            color: theme.textMuted
        }
        Text {
            Layout.alignment: Qt.AlignHCenter
            text: "No post selected"
            font.pixelSize: 18
            color: theme.textSecondary
        }
        Text {
            Layout.alignment: Qt.AlignHCenter
            text: "Select a post from the Content page to view details"
            font.pixelSize: 13
            color: theme.textMuted
        }
    }

    // ── Main content ─────────────────────────────────────────────
    ScrollView {
        anchors.fill: parent
        visible: !loading && postId
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            width: parent.width
            spacing: theme.spacingLg

            // ── Back button ────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: theme.pageMargin
                Layout.rightMargin: theme.pageMargin
                Layout.topMargin: theme.spacingMd

                Button {
                    text: "← Back"
                    flat: true
                    onClicked: {
                        if (typeof sidebar !== "undefined")
                            sidebar.navigate("content")
                    }
                }
                Item { Layout.fillWidth: true }
            }

            // ── Video preview ──────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: theme.pageMargin
                Layout.rightMargin: theme.pageMargin
                Layout.preferredHeight: 300
                color: theme.surface
                radius: theme.radiusMd
                border.color: theme.textMuted
                border.width: 1

                // Try video player, fall back to thumbnail
                Image {
                    id: videoPreview
                    anchors.fill: parent
                    anchors.margins: 1
                    source: {
                        if (videoPath && typeof controller !== "undefined" && controller.getThumbnail)
                            return controller.getThumbnail(videoPath)
                        return ""
                    }
                    fillMode: Image.PreserveAspectCrop
                    cache: false
                }

                // Play icon overlay
                Rectangle {
                    anchors.centerIn: parent
                    width: 64
                    height: 64
                    radius: 32
                    color: "#80000000"
                    visible: videoPreview.status === Image.Ready

                    Text {
                        anchors.centerIn: parent
                        text: Icons.play
                        font.family: theme.iconFontFamily
                        font.pixelSize: 28
                        color: "#ffffff"
                    }
                }

                // No preview fallback
                ColumnLayout {
                    anchors.centerIn: parent
                    visible: videoPreview.status !== Image.Ready && !videoPath
                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: Icons.content
                        font.family: theme.iconFontFamily
                        font.pixelSize: 48
                        color: theme.textMuted
                    }
                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "No preview available"
                        color: theme.textMuted
                        font.pixelSize: 13
                    }
                }
            }

            // ── Post metadata ──────────────────────────────────────
            ColumnLayout {
                Layout.fillWidth: true
                Layout.leftMargin: theme.pageMargin
                Layout.rightMargin: theme.pageMargin
                spacing: theme.spacingSm

                Text {
                    text: caption || "Untitled"
                    font.pixelSize: 20
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: theme.spacingMd

                    // Video ID
                    Text {
                        text: "ID: " + (postId || "—")
                        font.pixelSize: 12
                        color: theme.textMuted
                    }

                    // Source platform
                    Rectangle {
                        visible: sourcePlatform
                        width: sourceLabel.implicitWidth + 16
                        height: sourceLabel.implicitHeight + 6
                        radius: theme.radiusSm
                        color: theme.accentMuted

                        Text {
                            id: sourceLabel
                            anchors.centerIn: parent
                            text: sourcePlatform
                            font.pixelSize: 11
                            color: theme.accent
                            font.capitalization: Font.Capitalize
                        }
                    }
                    Item { Layout.fillWidth: true }
                }
            }

            // ── Platform tabs ──────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: theme.pageMargin
                Layout.rightMargin: theme.pageMargin
                Layout.preferredHeight: 48
                color: theme.surface
                radius: theme.radiusSm
                border.color: theme.textMuted
                border.width: 1

                TabBar {
                    id: platformTabs
                    anchors.fill: parent
                    background: Rectangle { color: "transparent" }

                    Repeater {
                        model: detailPage.platformList()

                        TabButton {
                            text: detailPage.platformDisplayName(modelData)
                            width: Math.max(120, implicitWidth)

                            background: Rectangle {
                                color: platformTabs.currentIndex === index ? theme.accentMuted : "transparent"
                                Rectangle {
                                    anchors.bottom: parent.bottom
                                    width: parent.width
                                    height: 2
                                    color: platformTabs.currentIndex === index ? theme.accent : "transparent"
                                }
                            }

                            contentItem: RowLayout {
                                spacing: 6
                                Text {
                                    text: detailPage.platformIcon(modelData)
                                    font.family: theme.iconFontFamily
                                    font.pixelSize: 14
                                    color: platformTabs.currentIndex === index ? theme.accent : theme.textSecondary
                                }
                                Text {
                                    text: detailPage.platformDisplayName(modelData)
                                    font.pixelSize: 13
                                    font.weight: platformTabs.currentIndex === index ? Font.DemiBold : Font.Normal
                                    color: platformTabs.currentIndex === index ? theme.accent : theme.textSecondary
                                }
                            }

                            onClicked: {
                                activeTab = modelData
                                platformTabs.currentIndex = index
                            }
                        }
                    }
                }
            }

            // ── Platform details ───────────────────────────────────
            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: theme.pageMargin
                Layout.rightMargin: theme.pageMargin
                Layout.preferredHeight: platformDetailsCol.implicitHeight + 32
                color: theme.surface
                radius: theme.radiusMd
                border.color: theme.textMuted
                border.width: 1

                ColumnLayout {
                    id: platformDetailsCol
                    anchors.fill: parent
                    anchors.margins: theme.spacingLg
                    spacing: theme.spacingMd

                    // Get the current platform data
                    property var currentData: detailPage.platformData(detailPage.platformList()[platformTabs.currentIndex] || "youtube")
                    property string currentPlatform: detailPage.platformList()[platformTabs.currentIndex] || "youtube"

                    // Post status
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd

                        Rectangle {
                            width: statusText.implicitWidth + 20
                            height: statusText.implicitHeight + 8
                            radius: theme.radiusSm
                            color: {
                                var d = platformDetailsCol.currentData
                                if (d.success === true) return "#e6f4ea"
                                if (d.success === false) return "#fce8e6"
                                return theme.surfaceAlt
                            }

                            Text {
                                id: statusText
                                anchors.centerIn: parent
                                text: {
                                    var d = platformDetailsCol.currentData
                                    if (d.success === true) return "✓ Posted"
                                    if (d.success === false) return "✗ Failed"
                                    return "— Not posted"
                                }
                                font.pixelSize: 12
                                font.weight: Font.Medium
                                color: {
                                    var d = platformDetailsCol.currentData
                                    if (d.success === true) return "#137333"
                                    if (d.success === false) return "#c5221f"
                                    return theme.textMuted
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }

                        // Post URL
                        Text {
                            visible: platformDetailsCol.currentData.post_url
                            text: Icons.external + " Open"
                            font.family: theme.iconFontFamily
                            font.pixelSize: 13
                            color: theme.accent
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: Qt.openUrlExternally(platformDetailsCol.currentData.post_url)
                            }
                        }
                    }

                    // Analytics metrics
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 4
                        columnSpacing: theme.spacingLg
                        rowSpacing: theme.spacingMd

                        // Views
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Text {
                                text: Icons.eye
                                font.family: theme.iconFontFamily
                                font.pixelSize: 18
                                color: theme.textSecondary
                            }
                            Text {
                                text: "Views"
                                font.pixelSize: 11
                                color: theme.textMuted
                            }
                            Text {
                                text: (platformDetailsCol.currentData.views || 0).toLocaleString(Qt.locale())
                                font.pixelSize: 18
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                        }

                        // Likes
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Text {
                                text: Icons.heart
                                font.family: theme.iconFontFamily
                                font.pixelSize: 18
                                color: theme.textSecondary
                            }
                            Text {
                                text: "Likes"
                                font.pixelSize: 11
                                color: theme.textMuted
                            }
                            Text {
                                text: (platformDetailsCol.currentData.likes || 0).toLocaleString(Qt.locale())
                                font.pixelSize: 18
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                        }

                        // Comments
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Text {
                                text: Icons.message
                                font.family: theme.iconFontFamily
                                font.pixelSize: 18
                                color: theme.textSecondary
                            }
                            Text {
                                text: "Comments"
                                font.pixelSize: 11
                                color: theme.textMuted
                            }
                            Text {
                                text: (platformDetailsCol.currentData.comments || 0).toLocaleString(Qt.locale())
                                font.pixelSize: 18
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                        }

                        // Shares
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Text {
                                text: Icons.share
                                font.family: theme.iconFontFamily
                                font.pixelSize: 18
                                color: theme.textSecondary
                            }
                            Text {
                                text: "Shares"
                                font.pixelSize: 11
                                color: theme.textMuted
                            }
                            Text {
                                text: (platformDetailsCol.currentData.shares || 0).toLocaleString(Qt.locale())
                                font.pixelSize: 18
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                        }
                    }

                    // Error message if failed
                    Rectangle {
                        visible: platformDetailsCol.currentData.error
                        Layout.fillWidth: true
                        Layout.preferredHeight: errorText.implicitHeight + 16
                        color: "#fce8e6"
                        radius: theme.radiusSm

                        Text {
                            id: errorText
                            anchors.fill: parent
                            anchors.margins: 8
                            text: "Error: " + (platformDetailsCol.currentData.error || "Unknown")
                            font.pixelSize: 12
                            color: "#c5221f"
                            wrapMode: Text.Wrap
                        }
                    }

                    // Delete button
                    RowLayout {
                        Layout.fillWidth: true
                        Item { Layout.fillWidth: true }

                        Button {
                            text: deleting ? "Deleting..." : "Delete from " + detailPage.platformDisplayName(platformDetailsCol.currentPlatform)
                            enabled: !deleting && platformDetailsCol.currentData.success === true
                            onClicked: detailPage.deletePost(platformDetailsCol.currentPlatform)

                            background: Rectangle {
                                color: parent.enabled ? (parent.down ? "#c5221f" : "#fce8e6") : theme.surfaceAlt
                                radius: theme.radiusSm
                            }

                            contentItem: Text {
                                text: parent.text
                                font.pixelSize: 13
                                color: parent.enabled ? "#c5221f" : theme.textMuted
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }
                }
            }

            // ── Spacer ─────────────────────────────────────────────
            Item { Layout.preferredHeight: theme.spacingLg }
        }
    }
}

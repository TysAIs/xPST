import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: dashboardPage
    background: Rectangle { color: theme.canvas }

    property var platformHealthData: {
        try {
            if (typeof controller !== "undefined" && controller.platformHealth)
                return JSON.parse(controller.platformHealth)
        } catch(e) {}
        return ({})
    }
    property var recentPostsData: {
        try {
            if (typeof controller !== "undefined" && controller.recentPosts)
                return JSON.parse(controller.recentPosts)
        } catch(e) {}
        return ([])
    }

    Component.onCompleted: {
        if (typeof controller !== "undefined")
            controller.refreshData()
    }

    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onDataChanged() {
            try {
                dashboardPage.platformHealthData = JSON.parse(controller.platformHealth)
                dashboardPage.recentPostsData = JSON.parse(controller.recentPosts)
            } catch(e) {}
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
                    text: "Dashboard"
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: "Dashboard page title"
                    Accessible.role: Accessible.Heading
                }
                Text {
                    text: "Overview of your cross-posting performance"
                    font.pixelSize: 13
                    color: theme.textSecondary
                }
            }

            // Metric cards row
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingXl

                Repeater {
                    model: [
                        { label: "Total Posts",     value: typeof controller !== "undefined" ? controller.totalPosts : "0",   icon: "P" },
                        { label: "Total Reach",     value: typeof controller !== "undefined" ? controller.totalReach : "0",   icon: "R" },
                        { label: "Best Platform",   value: typeof controller !== "undefined" ? controller.bestPlatform : "-", icon: "B" },
                        { label: "Posts This Week",  value: typeof controller !== "undefined" ? controller.postsThisWeek : "0", icon: "W" }
                    ]

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 120
                        radius: theme.radiusLg
                        color: theme.surfaceCard

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: theme.pageMargin
                            spacing: theme.spacingSm

                            RowLayout {
                                spacing: theme.spacingSm
                                Text {
                                    text: modelData.icon
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    color: theme.accent
                                }
                                Text {
                                    text: modelData.label
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                            }
                            Text {
                                text: String(modelData.value)
                                font.pixelSize: 20
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                        }
                    }
                }
            }

            // Platform Health - from real controller data
            ColumnLayout {
                spacing: theme.spacingMd
                Text {
                    text: "Platform Health"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: "Platform Health section"
                    Accessible.role: Accessible.Heading
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: theme.spacingXl

                    Repeater {
                        model: {
                            var result = []
                            var keys = ["youtube", "instagram", "x", "tiktok"]
                            for (var i = 0; i < keys.length; i++) {
                                var k = keys[i]
                                var info = dashboardPage.platformHealthData[k]
                                if (info) {
                                    result.push(info)
                                } else {
                                    result.push({ name: k, label: k.charAt(0).toUpperCase() + k.slice(1), status: "unknown" })
                                }
                            }
                            return result
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 80
                            radius: theme.radiusLg
                            color: theme.surfaceCard

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: theme.pageMargin
                                spacing: theme.spacingMd

                                Rectangle {
                                    width: 10; height: 10; radius: 5
                                    color: {
                                        var s = modelData.status || "unknown"
                                        if (s === "ok" || s === "healthy" || s === "connected") return theme.success
                                        if (s === "warning" || s === "degraded") return theme.warning
                                        if (s === "error" || s === "failed") return theme.error
                                        return theme.textMuted
                                    }
                                }
                                ColumnLayout {
                                    spacing: theme.spacingXs
                                    Text {
                                        text: modelData.label || modelData.name || ""
                                        font.pixelSize: 13
                                        font.weight: Font.DemiBold
                                        color: dashboardPage.platformColor(modelData.name)
                                    }
                                    Text {
                                        text: {
                                            var s = modelData.status || "unknown"
                                            if (s === "ok" || s === "healthy" || s === "connected") return "Connected"
                                            if (s === "warning" || s === "degraded") return "Degraded"
                                            if (s === "error" || s === "failed") return "Error"
                                            return "Unknown"
                                        }
                                        font.pixelSize: 12
                                        color: theme.textSecondary
                                    }
                                }
                                Item { Layout.fillWidth: true }
                            }
                        }
                    }
                }
            }

            // Recent Posts - from real controller data
            ColumnLayout {
                spacing: theme.spacingMd
                Text {
                    text: "Recent Posts"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: "Recent Posts section"
                    Accessible.role: Accessible.Heading
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: 3
                    columnSpacing: theme.spacingXl
                    rowSpacing: theme.spacingXl

                    Repeater {
                        model: {
                            // Show up to 6 recent posts from real data
                            var posts = dashboardPage.recentPostsData
                            if (!posts || !Array.isArray(posts)) return []
                            var result = []
                            for (var i = 0; i < Math.min(posts.length, 6); i++) {
                                var p = posts[i]
                                var ts = p.timestamp || ""
                                var timeLabel = ""
                                if (ts) {
                                    try {
                                        var d = new Date(ts)
                                        var now = new Date()
                                        var diff = (now - d) / 1000
                                        if (diff < 60) timeLabel = "just now"
                                        else if (diff < 3600) timeLabel = Math.floor(diff / 60) + "m ago"
                                        else if (diff < 86400) timeLabel = Math.floor(diff / 3600) + "h ago"
                                        else if (diff < 604800) timeLabel = Math.floor(diff / 86400) + "d ago"
                                        else timeLabel = d.toLocaleDateString()
                                    } catch(e) { timeLabel = ts }
                                }
                                result.push({
                                    title: p.title || p.postId || "Untitled",
                                    caption: p.caption || "",
                                    platform: p.platform || "",
                                    time: timeLabel,
                                    status: p.status || "posted"
                                })
                            }
                            return result
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 100
                            radius: theme.radiusLg
                            color: theme.surfaceCard

                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: theme.pageMargin
                                spacing: theme.spacingSm

                                Text {
                                    text: modelData.title
                                    font.pixelSize: 14
                                    font.weight: Font.DemiBold
                                    color: theme.textPrimary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                RowLayout {
                                    spacing: theme.spacingSm
                                    Rectangle {
                                        width: platformLabel.implicitWidth + theme.spacingMd
                                        height: platformLabel.implicitHeight + theme.spacingXs
                                        radius: theme.radiusSm
                                        color: dashboardPage.platformColor(modelData.platform)
                                        opacity: 0.2
                                        Text {
                                            id: platformLabel
                                            anchors.centerIn: parent
                                            text: modelData.platform
                                            font.pixelSize: 11
                                            font.weight: Font.DemiBold
                                            color: parent.color
                                            opacity: 5.0
                                        }
                                    }
                                    Text {
                                        text: modelData.time
                                        font.pixelSize: 11
                                        color: theme.textMuted
                                    }
                                }
                            }
                        }
                    }
                }

                // Empty state for recent posts
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 80
                    color: "transparent"
                    visible: !dashboardPage.recentPostsData || dashboardPage.recentPostsData.length === 0

                    ColumnLayout {
                        anchors.centerIn: parent
                        spacing: theme.spacingSm
                        Text {
                            text: "No recent posts"
                            font.pixelSize: 13
                            color: theme.textMuted
                            horizontalAlignment: Text.AlignHCenter
                            Layout.alignment: Qt.AlignHCenter
                        }
                    }
                }
            }
        }
    }
}

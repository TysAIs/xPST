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
        return []
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
        if (n === "x") return theme.xPlatform
        if (n === "tiktok") return theme.tiktok
        return theme.accent
    }

    function titleCase(value) {
        var text = String(value || "")
        if (text.length === 0) return "Unknown"
        return text.charAt(0).toUpperCase() + text.slice(1)
    }

    Flickable {
        anchors.fill: parent
        contentHeight: contentCol.implicitHeight + theme.pageMargin
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: contentCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: theme.pageMargin
            spacing: 24

            RowLayout {
                Layout.fillWidth: true
                spacing: 18

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 5

                    Text {
                        text: "Dashboard"
                        font.family: theme.fontFamily
                        font.pixelSize: 30
                        font.weight: Font.DemiBold
                        color: theme.textPrimary
                        Accessible.name: "Dashboard page title"
                        Accessible.role: Accessible.Heading
                    }

                    Text {
                        text: "Monitor content, platform health, and recent publishing activity."
                        font.family: theme.fontFamily
                        font.pixelSize: 14
                        color: theme.textSecondary
                    }
                }

                Rectangle {
                    Layout.preferredWidth: refreshText.implicitWidth + 28
                    Layout.preferredHeight: 34
                    radius: theme.radiusMd
                    color: refreshMouse.containsMouse ? theme.accentHover : theme.accent

                    Text {
                        id: refreshText
                        anchors.centerIn: parent
                        text: "Refresh"
                        font.family: theme.fontFamily
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        color: "white"
                    }

                    MouseArea {
                        id: refreshMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (typeof controller !== "undefined")
                                controller.refreshData()
                        }
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: dashboardPage.width < 960 ? 2 : 4
                columnSpacing: 14
                rowSpacing: 14

                Repeater {
                    model: [
                        { label: "Total posts", value: typeof controller !== "undefined" ? controller.totalPosts : "0", tone: theme.accent },
                        { label: "Total reach", value: typeof controller !== "undefined" ? controller.totalReach : "0", tone: theme.success },
                        { label: "Best platform", value: typeof controller !== "undefined" ? controller.bestPlatform : "-", tone: theme.instagram },
                        { label: "This week", value: typeof controller !== "undefined" ? controller.postsThisWeek : "0", tone: theme.warning }
                    ]

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 112
                        radius: theme.radiusXl
                        color: theme.surfaceCard
                        border.color: theme.separator
                        border.width: 1

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 18
                            spacing: 8

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8

                                Rectangle {
                                    Layout.preferredWidth: 8
                                    Layout.preferredHeight: 8
                                    radius: 4
                                    color: modelData.tone
                                }

                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.label
                                    font.family: theme.fontFamily
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                    elide: Text.ElideRight
                                }
                            }

                            Text {
                                Layout.fillWidth: true
                                text: String(modelData.value)
                                font.family: theme.fontFamily
                                font.pixelSize: 27
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: dashboardPage.width < 1040 ? 1 : 2
                columnSpacing: 18
                rowSpacing: 18

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 292
                    radius: theme.radiusXl
                    color: theme.surfaceCard
                    border.color: theme.separator

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 20
                        spacing: 14

                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                Layout.fillWidth: true
                                text: "Platform health"
                                font.family: theme.fontFamily
                                font.pixelSize: 17
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                                Accessible.name: "Platform Health section"
                                Accessible.role: Accessible.Heading
                            }
                            Text {
                                text: "Live status"
                                font.family: theme.fontFamily
                                font.pixelSize: 12
                                color: theme.textMuted
                            }
                        }

                        Repeater {
                            model: {
                                var result = []
                                var keys = ["youtube", "instagram", "x", "tiktok"]
                                for (var i = 0; i < keys.length; i++) {
                                    var k = keys[i]
                                    var info = dashboardPage.platformHealthData[k]
                                    if (info) {
                                        info.name = info.name || k
                                        info.label = info.label || dashboardPage.titleCase(k)
                                        result.push(info)
                                    } else {
                                        result.push({ name: k, label: dashboardPage.titleCase(k), status: "unknown" })
                                    }
                                }
                                return result
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 44
                                radius: theme.radiusMd
                                color: healthMouse.containsMouse ? theme.surfaceAlt : "transparent"

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: 2
                                    anchors.rightMargin: 2
                                    spacing: 12

                                    Rectangle {
                                        Layout.preferredWidth: 30
                                        Layout.preferredHeight: 30
                                        radius: 8
                                        color: dashboardPage.platformColor(modelData.name)
                                        opacity: 0.18
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.label || modelData.name || ""
                                        font.family: theme.fontFamily
                                        font.pixelSize: 13
                                        font.weight: Font.DemiBold
                                        color: theme.textPrimary
                                        elide: Text.ElideRight
                                    }

                                    Rectangle {
                                        Layout.preferredWidth: statusText.implicitWidth + 20
                                        Layout.preferredHeight: 26
                                        radius: 13
                                        color: {
                                            var s = modelData.status || "unknown"
                                            if (s === "ok" || s === "healthy" || s === "connected") return theme.success
                                            if (s === "warning" || s === "degraded") return theme.warning
                                            if (s === "error" || s === "failed") return theme.error
                                            return theme.surfaceAlt
                                        }
                                        opacity: modelData.status === "unknown" ? 1 : 0.22

                                        Text {
                                            id: statusText
                                            anchors.centerIn: parent
                                            text: {
                                                var s = modelData.status || "unknown"
                                                if (s === "ok" || s === "healthy" || s === "connected") return "Connected"
                                                if (s === "warning" || s === "degraded") return "Degraded"
                                                if (s === "error" || s === "failed") return "Error"
                                                return "Unknown"
                                            }
                                            font.family: theme.fontFamily
                                            font.pixelSize: 11
                                            font.weight: Font.DemiBold
                                            color: modelData.status === "unknown" ? theme.textMuted : theme.textPrimary
                                        }
                                    }
                                }

                                MouseArea {
                                    id: healthMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    acceptedButtons: Qt.NoButton
                                }
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 292
                    radius: theme.radiusXl
                    color: theme.surfaceCard
                    border.color: theme.separator

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 20
                        spacing: 14

                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                Layout.fillWidth: true
                                text: "Recent posts"
                                font.family: theme.fontFamily
                                font.pixelSize: 17
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                                Accessible.name: "Recent Posts section"
                                Accessible.role: Accessible.Heading
                            }
                            Text {
                                text: "Latest 5"
                                font.family: theme.fontFamily
                                font.pixelSize: 12
                                color: theme.textMuted
                            }
                        }

                        ListView {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 8
                            model: {
                                var posts = dashboardPage.recentPostsData
                                if (!posts || !Array.isArray(posts)) return []
                                var result = []
                                for (var i = 0; i < Math.min(posts.length, 5); i++) {
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
                                        platform: p.platform || "local",
                                        time: timeLabel || "queued",
                                        status: p.status || "posted"
                                    })
                                }
                                return result
                            }

                            delegate: Rectangle {
                                width: ListView.view ? ListView.view.width : 320
                                height: 46
                                radius: theme.radiusMd
                                color: postMouse.containsMouse ? theme.surfaceAlt : "transparent"

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: 2
                                    anchors.rightMargin: 2
                                    spacing: 12

                                    Rectangle {
                                        Layout.preferredWidth: 30
                                        Layout.preferredHeight: 30
                                        radius: 8
                                        color: dashboardPage.platformColor(modelData.platform)
                                    }

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 1
                                        Text {
                                            Layout.fillWidth: true
                                            text: modelData.title
                                            font.family: theme.fontFamily
                                            font.pixelSize: 13
                                            font.weight: Font.DemiBold
                                            color: theme.textPrimary
                                            elide: Text.ElideRight
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: dashboardPage.titleCase(modelData.platform) + " - " + modelData.time
                                            font.family: theme.fontFamily
                                            font.pixelSize: 11
                                            color: theme.textMuted
                                            elide: Text.ElideRight
                                        }
                                    }
                                }

                                MouseArea {
                                    id: postMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    acceptedButtons: Qt.NoButton
                                }
                            }

                            Text {
                                anchors.centerIn: parent
                                text: "No recent posts yet"
                                font.family: theme.fontFamily
                                font.pixelSize: 13
                                color: theme.textMuted
                                visible: !dashboardPage.recentPostsData || dashboardPage.recentPostsData.length === 0
                            }
                        }
                    }
                }
            }
        }
    }
}

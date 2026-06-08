import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: dashboardPage
    background: Rectangle { color: theme.canvas }

    Component.onCompleted: {
        if (typeof controller !== "undefined")
            controller.refreshData()
    }

    Flickable {
        anchors.fill: parent
        contentHeight: contentCol.implicitHeight + theme.spacing32
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: contentCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: theme.spacing24
            spacing: theme.spacing24

            // Header
            ColumnLayout {
                spacing: theme.spacing4
                Text {
                    text: "Dashboard"
                    font.pixelSize: 20
                    font.bold: true
                    color: theme.textPrimary
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
                        { label: "Total Posts",     value: typeof controller !== "undefined" ? controller.totalPosts : "0",   icon: "📝" },
                        { label: "Total Reach",     value: typeof controller !== "undefined" ? controller.totalReach : "0",   icon: "👥" },
                        { label: "Best Platform",   value: typeof controller !== "undefined" ? controller.bestPlatform : "—", icon: "🏆" },
                        { label: "Posts This Week",  value: typeof controller !== "undefined" ? controller.postsThisWeek : "0", icon: "📅" }
                    ]

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 120
                        radius: theme.radius12
                        color: theme.surfaceCard

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingXl
                            spacing: theme.spacingSm

                            RowLayout {
                                spacing: theme.spacingSm
                                Text { text: modelData.icon; font.pixelSize: 14 }
                                Text {
                                    text: modelData.label
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                            }
                            Text {
                                text: String(modelData.value)
                                font.pixelSize: 20
                                font.bold: true
                                color: theme.textPrimary
                            }
                        }
                    }
                }
            }

            // Platform Health
            ColumnLayout {
                spacing: theme.spacingMd
                Text {
                    text: "Platform Health"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: theme.spacingXl

                    Repeater {
                        model: [
                            { name: "YouTube",   color: theme.youtube,    status: "healthy" },
                            { name: "Instagram", color: theme.instagram,  status: "healthy" },
                            { name: "X",         color: theme.xtwitter,  status: "healthy" },
                            { name: "TikTok",    color: theme.tiktok,     status: "warning" }
                        ]

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 80
                            radius: theme.radius12
                            color: theme.surfaceCard

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: theme.spacingXl
                                spacing: theme.spacingMd

                                Rectangle {
                                    width: 10; height: 10; radius: 5
                                    color: modelData.status === "healthy" ? theme.success : theme.warning
                                }
                                ColumnLayout {
                                    spacing: theme.spacing4
                                    Text {
                                        text: modelData.name
                                        font.pixelSize: 13
                                        font.bold: true
                                        color: modelData.color
                                    }
                                    Text {
                                        text: modelData.status === "healthy" ? "Connected" : "Attention"
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

            // Recent Posts
            ColumnLayout {
                spacing: theme.spacingMd
                Text {
                    text: "Recent Posts"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: 3
                    columnSpacing: theme.spacingXl
                    rowSpacing: theme.spacingXl

                    Repeater {
                        model: [
                            { title: "Short #42", platform: "YouTube", time: "2 hours ago" },
                            { title: "Reel #18",  platform: "Instagram", time: "5 hours ago" },
                            { title: "Clip #7",   platform: "TikTok", time: "1 day ago" },
                            { title: "Short #41", platform: "YouTube", time: "1 day ago" },
                            { title: "Post #99",  platform: "X", time: "2 days ago" },
                            { title: "Reel #17",  platform: "Instagram", time: "3 days ago" }
                        ]

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 100
                            radius: theme.radius12
                            color: theme.surfaceCard

                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: theme.spacingXl
                                spacing: theme.spacingSm

                                Text {
                                    text: modelData.title
                                    font.pixelSize: 14
                                    font.bold: true
                                    color: theme.textPrimary
                                }
                                RowLayout {
                                    spacing: theme.spacingSm
                                    Rectangle {
                                        width: platformLabel.implicitWidth + theme.spacingMd
                                        height: platformLabel.implicitHeight + theme.spacing4
                                        radius: theme.radius6
                                        color: modelData.platform === "YouTube" ? theme.youtube
                                             : modelData.platform === "Instagram" ? theme.instagram
                                             : modelData.platform === "X" ? theme.xtwitter
                                             : theme.tiktok
                                        opacity: 0.2
                                        Text {
                                            id: platformLabel
                                            anchors.centerIn: parent
                                            text: modelData.platform
                                            font.pixelSize: 11
                                            font.bold: true
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
            }
        }
    }
}

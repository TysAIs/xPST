import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: analyticsPage
    background: Rectangle { color: theme.canvas }

    property string activePlatform: "All"
    property var analyticsJson: {
        try {
            if (typeof controller !== "undefined" && controller.analyticsData)
                return JSON.parse(controller.analyticsData)
        } catch(e) {}
        return ({"available": false})
    }

    property bool hasData: analyticsJson.available === true &&
                           analyticsJson.summary &&
                           (analyticsJson.summary.total_views > 0 ||
                            analyticsJson.summary.total_likes > 0 ||
                            analyticsJson.summary.total_comments > 0 ||
                            analyticsJson.summary.total_shares > 0)

    property var platformData: {
        if (!analyticsJson.platforms) return []
        var filtered = []
        for (var i = 0; i < analyticsJson.platforms.length; i++) {
            var p = analyticsJson.platforms[i]
            if (activePlatform === "All" || p.platform === activePlatform.toLowerCase())
                filtered.push(p)
        }
        return filtered
    }

    // Refresh when data changes
    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onDataChanged() {
            try {
                analyticsPage.analyticsJson = JSON.parse(controller.analyticsData)
            } catch(e) {}
        }
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
            anchors.margins: theme.spacingXl
            spacing: theme.spacingXl

            // Header
            ColumnLayout {
                spacing: theme.spacingXs
                Text {
                    text: "Analytics"
                    font.pixelSize: 20
                    font.bold: true
                    color: theme.textPrimary
                }
                Text {
                    text: "Performance metrics across platforms"
                    font.pixelSize: 13
                    color: theme.textSecondary
                }
            }

            // Platform tabs
            RowLayout {
                spacing: theme.spacingSm

                Repeater {
                    model: ["All", "YouTube", "Instagram", "X", "TikTok"]

                    Rectangle {
                        width: tabLabel.implicitWidth + theme.spacingXl
                        height: 36
                        radius: theme.radiusMd
                        color: analyticsPage.activePlatform === modelData ? theme.accent : theme.surfaceCard

                        Text {
                            id: tabLabel
                            anchors.centerIn: parent
                            text: modelData
                            font.pixelSize: 12
                            font.weight: analyticsPage.activePlatform === modelData ? Font.DemiBold : Font.Normal
                            color: analyticsPage.activePlatform === modelData ? "#ffffff" : theme.textSecondary
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: analyticsPage.activePlatform = modelData
                        }
                    }
                }
            }

            // Empty state
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 200
                radius: theme.radiusLg
                color: theme.surfaceCard
                visible: !analyticsPage.hasData

                ColumnLayout {
                    anchors.centerIn: parent
                    spacing: theme.spacingMd

                    Text {
                        text: "📊"
                        font.pixelSize: 48
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "No analytics data yet"
                        font.pixelSize: 16
                        font.bold: true
                        color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "Post some content first to see analytics here"
                        font.pixelSize: 13
                        color: theme.textMuted
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                }
            }

            // Metric cards (visible when data exists)
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingXl
                visible: analyticsPage.hasData

                Repeater {
                    model: [
                        {
                            label: "Views",
                            value: analyticsPage.analyticsJson.summary
                                   ? (analyticsPage.analyticsJson.summary.total_views || 0).toLocaleString()
                                   : "0",
                            icon: "👁",
                            color: "#6366f1"
                        },
                        {
                            label: "Likes",
                            value: analyticsPage.analyticsJson.summary
                                   ? (analyticsPage.analyticsJson.summary.total_likes || 0).toLocaleString()
                                   : "0",
                            icon: "❤️",
                            color: "#ef4444"
                        },
                        {
                            label: "Comments",
                            value: analyticsPage.analyticsJson.summary
                                   ? (analyticsPage.analyticsJson.summary.total_comments || 0).toLocaleString()
                                   : "0",
                            icon: "💬",
                            color: "#f59e0b"
                        },
                        {
                            label: "Shares",
                            value: analyticsPage.analyticsJson.summary
                                   ? (analyticsPage.analyticsJson.summary.total_shares || 0).toLocaleString()
                                   : "0",
                            icon: "🔄",
                            color: "#22c55e"
                        }
                    ]

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 120
                        radius: theme.radiusLg
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
                                text: modelData.value
                                font.pixelSize: 20
                                font.bold: true
                                color: theme.textPrimary
                            }
                            // Small accent bar
                            Rectangle {
                                Layout.preferredWidth: 40
                                Layout.preferredHeight: 3
                                radius: 2
                                color: modelData.color
                            }
                        }
                    }
                }
            }

            // Per-platform breakdown
            ColumnLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd
                visible: analyticsPage.hasData && analyticsPage.platformData.length > 0

                Text {
                    text: "Platform Breakdown"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Repeater {
                    model: analyticsPage.platformData

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 80
                        radius: theme.radiusMd
                        color: theme.surfaceCard

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingXl
                            spacing: theme.spacingXl

                            // Platform indicator
                            Rectangle {
                                width: 40; height: 40
                                radius: theme.radiusMd
                                color: {
                                    var p = (modelData.platform || "").toLowerCase()
                                    if (p === "youtube") return Qt.rgba(1, 0, 0, 0.15)
                                    if (p === "instagram") return Qt.rgba(0.88, 0.19, 0.42, 0.15)
                                    if (p === "x") return Qt.rgba(0.11, 0.61, 0.94, 0.15)
                                    if (p === "tiktok") return Qt.rgba(0, 0.95, 0.92, 0.15)
                                    return theme.accentMuted
                                }
                                Text {
                                    anchors.centerIn: parent
                                    text: {
                                        var p = (modelData.platform || "").toLowerCase()
                                        if (p === "youtube") return "▶️"
                                        if (p === "instagram") return "📷"
                                        if (p === "x") return "𝕏"
                                        if (p === "tiktok") return "🎵"
                                        return "📱"
                                    }
                                    font.pixelSize: 16
                                }
                            }

                            ColumnLayout {
                                spacing: 2
                                Text {
                                    text: (modelData.platform || "Unknown").charAt(0).toUpperCase() + (modelData.platform || "Unknown").slice(1)
                                    font.pixelSize: 14
                                    font.bold: true
                                    color: theme.textPrimary
                                }
                                Text {
                                    text: (modelData.post_count || 0) + " posts"
                                    font.pixelSize: 11
                                    color: theme.textMuted
                                }
                            }

                            Item { Layout.fillWidth: true }

                            ColumnLayout {
                                spacing: 2
                                Text {
                                    text: (modelData.total_views || 0).toLocaleString() + " views"
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                    horizontalAlignment: Text.AlignRight
                                }
                                Text {
                                    text: (modelData.total_likes || 0).toLocaleString() + " likes"
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                    horizontalAlignment: Text.AlignRight
                                }
                            }
                        }
                    }
                }
            }

            // Top posts section
            ColumnLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd
                visible: analyticsPage.hasData &&
                         analyticsPage.analyticsJson.top_posts &&
                         analyticsPage.analyticsJson.top_posts.length > 0

                Text {
                    text: "Top Posts"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Repeater {
                    model: analyticsPage.analyticsJson.top_posts || []

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 60
                        radius: theme.radiusMd
                        color: theme.surfaceCard

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingLg
                            spacing: theme.spacingMd

                            Text {
                                text: "🏆"
                                font.pixelSize: 16
                            }

                            ColumnLayout {
                                spacing: 2
                                Layout.fillWidth: true
                                Text {
                                    text: modelData.title || modelData.video_id || "Untitled"
                                    font.pixelSize: 13
                                    font.bold: true
                                    color: theme.textPrimary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: (modelData.platform || "") + " • " + (modelData.views || 0).toLocaleString() + " views"
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

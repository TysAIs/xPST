import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Page {
    id: analyticsPage
    background: Rectangle { color: theme.canvas }

    property string activePlatform: "All"
    property string dateRange: "all"  // "week", "month", "all"
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

    // Bar chart data: per-platform metrics for Canvas rendering
    property var chartPlatforms: {
        var result = []
        var data = platformData
        for (var i = 0; i < data.length; i++) {
            result.push({
                label: (data[i].platform || "").charAt(0).toUpperCase() + (data[i].platform || "").slice(1),
                views: data[i].total_views || 0,
                likes: data[i].total_likes || 0,
                comments: data[i].total_comments || 0,
                shares: data[i].total_shares || 0,
                color: {
                    var p = (data[i].platform || "").toLowerCase()
                    if (p === "youtube") return theme.youtube
                    if (p === "instagram") return theme.instagram
                    if (p === "x") return theme.xtwitter
                    if (p === "tiktok") return theme.tiktok
                    return theme.accent
                }
            })
        }
        return result
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
                    Accessible.name: "Analytics page title"
                    Accessible.role: Accessible.Heading
                }
                Text {
                    text: "Performance metrics across platforms"
                    font.pixelSize: 13
                    color: theme.textSecondary
                }
            }

            // Date range picker (Item 11)
            RowLayout {
                spacing: theme.spacingMd

                Text {
                    text: "Period:"
                    font.pixelSize: 12
                    color: theme.textMuted
                }

                Repeater {
                    model: [
                        { label: "Week",  value: "week" },
                        { label: "Month", value: "month" },
                        { label: "All",   value: "all" }
                    ]

                    Rectangle {
                        width: rangeLabel.implicitWidth + theme.spacingXl
                        height: 30
                        radius: theme.radiusMd
                        color: analyticsPage.dateRange === modelData.value ? theme.accent : theme.surfaceCard
                        border.color: analyticsPage.dateRange === modelData.value ? theme.accent : "transparent"
                        border.width: 1

                        Text {
                            id: rangeLabel
                            anchors.centerIn: parent
                            text: modelData.label
                            font.pixelSize: 12
                            font.weight: analyticsPage.dateRange === modelData.value ? Font.DemiBold : Font.Normal
                            color: analyticsPage.dateRange === modelData.value ? "#ffffff" : theme.textSecondary
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: analyticsPage.dateRange = modelData.value
                            Accessible.name: "Set date range to " + modelData.label
                            Accessible.role: Accessible.Button
                        }
                    }
                }

                Item { Layout.fillWidth: true }
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
                            Accessible.name: "Filter by " + modelData + " platform"
                            Accessible.role: Accessible.Button
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

            // Bar chart (Item 3)
            ColumnLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd
                visible: analyticsPage.hasData && analyticsPage.chartPlatforms.length > 0

                Text {
                    text: "Engagement by Platform"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                // Legend
                RowLayout {
                    spacing: theme.spacingLg
                    Repeater {
                        model: [
                            { label: "Views", color: "#6366f1" },
                            { label: "Likes", color: "#ef4444" },
                            { label: "Comments", color: "#f59e0b" },
                            { label: "Shares", color: "#22c55e" }
                        ]
                        RowLayout {
                            spacing: theme.spacingXs
                            Rectangle { width: 10; height: 10; radius: 2; color: modelData.color }
                            Text {
                                text: modelData.label
                                font.pixelSize: 11
                                color: theme.textSecondary
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 240
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    Canvas {
                        id: barChart
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl

                        property var platforms: analyticsPage.chartPlatforms
                        property color bgColor: theme.surfaceCard
                        property color textColor: theme.textPrimary
                        property color gridColor: theme.surfaceAlt

                        onPaint: {
                            var ctx = getContext("2d")
                            ctx.reset()
                            ctx.clearRect(0, 0, width, height)

                            var data = platforms
                            if (!data || data.length === 0) return

                            // Find max value across all metrics
                            var maxVal = 1
                            for (var i = 0; i < data.length; i++) {
                                maxVal = Math.max(maxVal, data[i].views, data[i].likes, data[i].comments, data[i].shares)
                            }

                            var padding = { left: 50, right: 20, top: 20, bottom: 40 }
                            var chartW = width - padding.left - padding.right
                            var chartH = height - padding.top - padding.bottom

                            // Draw grid lines
                            ctx.strokeStyle = gridColor
                            ctx.lineWidth = 0.5
                            var gridSteps = 5
                            for (var g = 0; g <= gridSteps; g++) {
                                var gy = padding.top + (chartH / gridSteps) * g
                                ctx.beginPath()
                                ctx.moveTo(padding.left, gy)
                                ctx.lineTo(width - padding.right, gy)
                                ctx.stroke()

                                // Y-axis labels
                                var val = Math.round(maxVal * (1 - g / gridSteps))
                                ctx.fillStyle = textColor
                                ctx.font = "10px sans-serif"
                                ctx.textAlign = "right"
                                ctx.fillText(val.toLocaleString(), padding.left - 8, gy + 4)
                            }

                            // Draw grouped bars
                            var groupCount = data.length
                            var metrics = ["views", "likes", "comments", "shares"]
                            var colors = ["#6366f1", "#ef4444", "#f59e0b", "#22c55e"]
                            var barCount = metrics.length
                            var groupWidth = chartW / groupCount
                            var barWidth = Math.min(16, (groupWidth - 20) / barCount)
                            var groupGap = 20

                            for (var i = 0; i < data.length; i++) {
                                var groupX = padding.left + groupWidth * i + groupGap / 2

                                // Platform label
                                ctx.fillStyle = textColor
                                ctx.font = "bold 11px sans-serif"
                                ctx.textAlign = "center"
                                ctx.fillText(data[i].label, groupX + (barWidth * barCount + (barCount - 1) * 2) / 2, height - padding.bottom + 18)

                                for (var m = 0; m < metrics.length; m++) {
                                    var val = data[i][metrics[m]] || 0
                                    var barH = maxVal > 0 ? (val / maxVal) * chartH : 0
                                    var bx = groupX + m * (barWidth + 2)
                                    var by = padding.top + chartH - barH

                                    ctx.fillStyle = colors[m]
                                    ctx.beginPath()
                                    ctx.roundRect(bx, by, barWidth, barH, [2, 2, 0, 0])
                                    ctx.fill()
                                }
                            }
                        }

                        // Re-paint when data changes
                        Connections {
                            target: analyticsPage
                            function onChartPlatformsChanged() { barChart.requestPaint() }
                        }
                        Connections {
                            target: theme
                            function onDarkModeChanged() { barChart.requestPaint() }
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

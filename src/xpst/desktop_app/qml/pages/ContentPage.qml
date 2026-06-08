import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: contentPage
    background: Rectangle { color: theme.canvas }

    property string searchQuery: ""
    property string activeFilter: "All"

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
            Text {
                text: "Content Library"
                font.pixelSize: 20
                font.bold: true
                color: theme.textPrimary
            }

            // Search bar
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                radius: theme.radiusMd
                color: theme.surfaceCard

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: theme.spacingXl
                    anchors.rightMargin: theme.spacingXl
                    spacing: theme.spacingSm

                    Text { text: "🔍"; font.pixelSize: 13 }
                    TextField {
                        id: searchField
                        Layout.fillWidth: true
                        placeholderText: "Search posts..."
                        color: theme.textPrimary
                        placeholderTextColor: theme.textMuted
                        background: null
                        font.pixelSize: 13
                        onTextChanged: contentPage.searchQuery = text
                    }
                }
            }

            // Filter pills
            RowLayout {
                spacing: theme.spacingSm

                Repeater {
                    model: ["All", "YouTube", "Instagram", "X", "TikTok"]

                    Rectangle {
                        width: filterLabel.implicitWidth + theme.spacingXl
                        height: 32
                        radius: theme.radiusXl
                        color: contentPage.activeFilter === modelData ? theme.accent : theme.surfaceCard
                        border.color: contentPage.activeFilter === modelData ? theme.accent : "transparent"
                        border.width: 1

                        Text {
                            id: filterLabel
                            anchors.centerIn: parent
                            text: modelData
                            font.pixelSize: 12
                            color: contentPage.activeFilter === modelData ? "#ffffff" : theme.textSecondary
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: contentPage.activeFilter = modelData
                        }
                    }
                }
            }

            // Content grid
            GridLayout {
                Layout.fillWidth: true
                columns: 3
                columnSpacing: theme.spacingXl
                rowSpacing: theme.spacingXl

                Repeater {
                    id: contentRepeater
                    model: typeof postModel !== "undefined" ? postModel : 0

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 220
                        radius: theme.radiusLg
                        color: theme.surfaceCard

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingXl
                            spacing: theme.spacingSm

                            // Thumbnail area
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 100
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                clip: true

                                // Try to load an actual thumbnail
                                Image {
                                    id: thumbImage
                                    anchors.fill: parent
                                    fillMode: Image.PreserveAspectCrop
                                    visible: status === Image.Ready
                                    source: {
                                        if (typeof controller !== "undefined" && model.thumbnail) {
                                            return controller.getThumbnail(model.thumbnail)
                                        }
                                        return ""
                                    }
                                    asynchronous: true

                                    Rectangle {
                                        anchors.fill: parent
                                        color: "transparent"
                                        border.color: theme.surfaceAlt
                                        border.width: 1
                                        radius: theme.radiusMd
                                    }
                                }

                                // Fallback icon when no thumbnail
                                Text {
                                    anchors.centerIn: parent
                                    text: "🎬"
                                    font.pixelSize: 24
                                    color: theme.textMuted
                                    visible: thumbImage.status !== Image.Ready
                                }
                            }

                            Text {
                                text: model.title || "Untitled"
                                font.pixelSize: 13
                                font.bold: true
                                color: theme.textPrimary
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                                maximumLineCount: 1
                            }

                            Text {
                                text: model.caption || ""
                                font.pixelSize: 11
                                color: theme.textSecondary
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                                maximumLineCount: 1
                                visible: text.length > 0
                            }

                            RowLayout {
                                spacing: theme.spacingSm
                                Rectangle {
                                    width: pLabel.implicitWidth + theme.spacingMd
                                    height: pLabel.implicitHeight + theme.spacingXs
                                    radius: theme.radiusSm
                                    color: {
                                        var p = (model.platform || "").toLowerCase()
                                        if (p === "youtube") return Qt.rgba(1, 0, 0, 0.15)
                                        if (p === "instagram") return Qt.rgba(0.88, 0.19, 0.42, 0.15)
                                        if (p === "x") return Qt.rgba(0.11, 0.61, 0.94, 0.15)
                                        if (p === "tiktok") return Qt.rgba(0, 0.95, 0.92, 0.15)
                                        return theme.accentMuted
                                    }
                                    Text {
                                        id: pLabel
                                        anchors.centerIn: parent
                                        text: model.platform || ""
                                        font.pixelSize: 11
                                        font.bold: true
                                        color: {
                                            var p = (model.platform || "").toLowerCase()
                                            if (p === "youtube") return theme.youtube
                                            if (p === "instagram") return theme.instagram
                                            if (p === "x") return theme.xtwitter
                                            if (p === "tiktok") return theme.tiktok
                                            return theme.accent
                                        }
                                    }
                                }

                                Rectangle {
                                    width: statusLabel.implicitWidth + theme.spacingMd
                                    height: statusLabel.implicitHeight + theme.spacingXs
                                    radius: theme.radiusSm
                                    color: model.status === "posted" ? Qt.rgba(0.13, 0.77, 0.37, 0.15) : theme.accentMuted
                                    Text {
                                        id: statusLabel
                                        anchors.centerIn: parent
                                        text: model.status || "unknown"
                                        font.pixelSize: 10
                                        color: model.status === "posted" ? theme.success : theme.accent
                                    }
                                }

                                Item { Layout.fillWidth: true }

                                Text {
                                    text: {
                                        var ts = model.timestamp || ""
                                        if (!ts) return ""
                                        try {
                                            var d = new Date(ts)
                                            var now = new Date()
                                            var diff = (now - d) / 1000
                                            if (diff < 60) return "just now"
                                            if (diff < 3600) return Math.floor(diff / 60) + "m ago"
                                            if (diff < 86400) return Math.floor(diff / 3600) + "h ago"
                                            if (diff < 604800) return Math.floor(diff / 86400) + "d ago"
                                            return d.toLocaleDateString()
                                        } catch(e) { return ts }
                                    }
                                    font.pixelSize: 11
                                    color: theme.textMuted
                                }
                            }
                        }
                    }
                }
            }

            // Empty state
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 200
                color: "transparent"
                visible: typeof postModel !== "undefined" && postModel.rowCount() === 0

                ColumnLayout {
                    anchors.centerIn: parent
                    spacing: theme.spacingMd

                    Text {
                        text: "📭"
                        font.pixelSize: 36
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "No content yet"
                        font.pixelSize: 16
                        font.bold: true
                        color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "Post some content to see it here"
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

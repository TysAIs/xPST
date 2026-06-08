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
                    model: typeof postModel !== "undefined" ? postModel : 6

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 200
                        radius: theme.radiusLg
                        color: theme.surfaceCard

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingXl
                            spacing: theme.spacingSm

                            // Thumbnail placeholder
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 80
                                radius: theme.radiusMd
                                color: theme.surfaceAlt

                                Text {
                                    anchors.centerIn: parent
                                    text: "🎬"
                                    font.pixelSize: 18
                                    color: theme.textMuted
                                }
                            }

                            Text {
                                text: typeof postModel !== "undefined" ? (model.title || "") : "Sample Post Title"
                                font.pixelSize: 13
                                font.bold: true
                                color: theme.textPrimary
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }

                            RowLayout {
                                spacing: theme.spacingSm
                                Rectangle {
                                    width: pLabel.implicitWidth + theme.spacingMd
                                    height: pLabel.implicitHeight + theme.spacingXs
                                    radius: theme.radiusSm
                                    color: theme.accentMuted
                                    Text {
                                        id: pLabel
                                        anchors.centerIn: parent
                                        text: typeof postModel !== "undefined" ? (model.platform || "") : "YouTube"
                                        font.pixelSize: 11
                                        color: theme.accent
                                    }
                                }
                                Text {
                                    text: typeof postModel !== "undefined" ? (model.status || "") : "Published"
                                    font.pixelSize: 11
                                    color: theme.textMuted
                                }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: typeof postModel !== "undefined" ? (model.timestamp || "") : "2h ago"
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

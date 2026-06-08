import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: analyticsPage
    background: Rectangle { color: theme.canvas }

    property string activePlatform: "All"

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

            // Metric cards
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingXl

                Repeater {
                    model: [
                        { label: "Views",    value: "12.4K", icon: "👁" },
                        { label: "Likes",    value: "3.2K",  icon: "❤️" },
                        { label: "Comments", value: "847",   icon: "💬" },
                        { label: "Shares",   value: "1.1K",  icon: "🔄" }
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
                        }
                    }
                }
            }

            // Chart placeholder
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 300
                radius: theme.radiusLg
                color: theme.surfaceCard

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
                        text: "Charts coming soon"
                        font.pixelSize: 14
                        color: theme.textMuted
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "Interactive charts will be available in the next update"
                        font.pixelSize: 12
                        color: theme.textMuted
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                }
            }
        }
    }
}

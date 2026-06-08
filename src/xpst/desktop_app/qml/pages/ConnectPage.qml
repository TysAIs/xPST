import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: connectPage
    background: Rectangle { color: theme.canvas }

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
                    text: "Connect Platforms"
                    font.pixelSize: 20
                    font.bold: true
                    color: theme.textPrimary
                }
                Text {
                    text: "Manage your social media connections"
                    font.pixelSize: 13
                    color: theme.textSecondary
                }
            }

            // Platform cards
            GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: theme.spacingXl
                rowSpacing: theme.spacingXl

                Repeater {
                    model: [
                        { name: "YouTube",   icon: "▶️", color: theme.youtube,    connected: true,  health: "healthy" },
                        { name: "Instagram", icon: "📷", color: theme.instagram,  connected: true,  health: "healthy" },
                        { name: "X",         icon: "𝕏",  color: theme.xtwitter,  connected: false, health: "unknown" },
                        { name: "TikTok",    icon: "🎵", color: theme.tiktok,     connected: true,  health: "warning" }
                    ]

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 180
                        radius: theme.radiusXl
                        color: theme.surfaceCard

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingXl
                            spacing: theme.spacingMd

                            RowLayout {
                                spacing: theme.spacingMd

                                Rectangle {
                                    width: 48; height: 48; radius: theme.radiusLg
                                    color: Qt.rgba(modelData.color.r, modelData.color.g, modelData.color.b, 0.15)
                                    Text {
                                        anchors.centerIn: parent
                                        text: modelData.icon
                                        font.pixelSize: 18
                                    }
                                }

                                ColumnLayout {
                                    spacing: theme.spacingXs
                                    Text {
                                        text: modelData.name
                                        font.pixelSize: 16
                                        font.bold: true
                                        color: theme.textPrimary
                                    }
                                    RowLayout {
                                        spacing: theme.spacingXs
                                        Rectangle {
                                            width: 8; height: 8; radius: 4
                                            color: modelData.connected ? theme.success : theme.textMuted
                                        }
                                        Text {
                                            text: modelData.connected ? "Connected" : "Not connected"
                                            font.pixelSize: 12
                                            color: theme.textSecondary
                                        }
                                    }
                                }
                                Item { Layout.fillWidth: true }
                            }

                            RowLayout {
                                spacing: theme.spacingSm
                                Text {
                                    text: "Health:"
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    color: modelData.health === "healthy" ? theme.success
                                         : modelData.health === "warning" ? theme.warning
                                         : theme.textMuted
                                }
                                Text {
                                    text: modelData.health === "healthy" ? "Good"
                                         : modelData.health === "warning" ? "Rate limited"
                                         : "Unknown"
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                }
                                Item { Layout.fillWidth: true }
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 36
                                radius: theme.radiusMd
                                color: modelData.connected ? theme.surfaceAlt : theme.accent

                                Text {
                                    anchors.centerIn: parent
                                    text: modelData.connected ? "Disconnect" : "Connect"
                                    font.pixelSize: 13
                                    font.bold: true
                                    color: modelData.connected ? theme.textSecondary : "#ffffff"
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        if (typeof controller !== "undefined")
                                            controller.connectPlatform(modelData.name)
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

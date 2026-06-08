import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: settingsPage
    background: Rectangle { color: theme.canvas }

    property string tiktokUsername: typeof controller !== "undefined" && controller.configData ? (JSON.parse(controller.configData).tiktok_username || "") : ""
    property string downloadDir: typeof controller !== "undefined" && controller.configData ? (JSON.parse(controller.configData).download_dir || "") : ""
    property bool youtubeEnabled: true
    property bool instagramEnabled: true
    property bool xEnabled: true
    property bool tiktokEnabled: true
    property int rateLimitPosts: 10
    property int rateLimitMinutes: 60

    Flickable {
        anchors.fill: parent
        contentHeight: settingsCol.implicitHeight + theme.spacingXxl
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: settingsCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: theme.spacingXl
            spacing: theme.spacingXl

            // Header
            Text {
                text: "Settings"
                font.pixelSize: 20
                font.bold: true
                color: theme.textPrimary
            }

            // General Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "General"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: generalCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: generalCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingXl

                        ColumnLayout {
                            spacing: theme.spacingXs
                            Text {
                                text: "TikTok Username"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1
                                TextInput {
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingMd
                                    text: settingsPage.tiktokUsername
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    clip: true
                                    onTextChanged: settingsPage.tiktokUsername = text
                                }
                            }
                        }

                        ColumnLayout {
                            spacing: theme.spacingXs
                            Text {
                                text: "Download Directory"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1
                                TextInput {
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingMd
                                    text: settingsPage.downloadDir
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    clip: true
                                    onTextChanged: settingsPage.downloadDir = text
                                }
                            }
                        }
                    }
                }
            }

            // Platforms Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Platforms"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: platformsCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: platformsCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingMd

                        Repeater {
                            model: [
                                { label: "YouTube",   prop: "youtubeEnabled",   color: theme.youtube },
                                { label: "Instagram", prop: "instagramEnabled", color: theme.instagram },
                                { label: "X",         prop: "xEnabled",         color: theme.xtwitter },
                                { label: "TikTok",    prop: "tiktokEnabled",    color: theme.tiktok }
                            ]

                            RowLayout {
                                spacing: theme.spacingMd
                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    color: modelData.color
                                }
                                Text {
                                    text: modelData.label
                                    font.pixelSize: 13
                                    color: theme.textPrimary
                                    Layout.fillWidth: true
                                }
                                Switch {
                                    checked: settingsPage[modelData.prop]
                                    onCheckedChanged: settingsPage[modelData.prop] = checked
                                }
                            }
                        }
                    }
                }
            }

            // Rate Limits Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Rate Limits"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: rateCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: rateCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingXl

                        ColumnLayout {
                            spacing: theme.spacingXs
                            Text {
                                text: "Max Posts per Window"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1
                                TextInput {
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingMd
                                    text: String(settingsPage.rateLimitPosts)
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    onTextChanged: {
                                        var v = parseInt(text)
                                        if (!isNaN(v)) settingsPage.rateLimitPosts = v
                                    }
                                }
                            }
                        }

                        ColumnLayout {
                            spacing: theme.spacingXs
                            Text {
                                text: "Window Duration (minutes)"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1
                                TextInput {
                                    anchors.fill: parent
                                    anchors.margins: theme.spacingMd
                                    text: String(settingsPage.rateLimitMinutes)
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    onTextChanged: {
                                        var v = parseInt(text)
                                        if (!isNaN(v)) settingsPage.rateLimitMinutes = v
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Notifications Section
            ColumnLayout {
                spacing: theme.spacingXl

                Text {
                    text: "Notifications"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: notifCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: notifCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingMd

                        RowLayout {
                            Text {
                                text: "Post completion alerts"
                                font.pixelSize: 13
                                color: theme.textPrimary
                                Layout.fillWidth: true
                            }
                            Switch { checked: true }
                        }
                        RowLayout {
                            Text {
                                text: "Error notifications"
                                font.pixelSize: 13
                                color: theme.textPrimary
                                Layout.fillWidth: true
                            }
                            Switch { checked: true }
                        }
                    }
                }
            }

            // Buttons
            RowLayout {
                spacing: theme.spacingMd
                Layout.topMargin: theme.spacingSm

                Rectangle {
                    width: saveLabel.implicitWidth + theme.spacingXxl
                    height: 40
                    radius: theme.radiusMd
                    color: theme.accent

                    Text {
                        id: saveLabel
                        anchors.centerIn: parent
                        text: "Save Settings"
                        font.pixelSize: 13
                        font.bold: true
                        color: "#ffffff"
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (typeof controller !== "undefined") {
                                var cfg = {
                                    tiktok_username: settingsPage.tiktokUsername,
                                    download_dir: settingsPage.downloadDir,
                                    youtube_enabled: settingsPage.youtubeEnabled,
                                    instagram_enabled: settingsPage.instagramEnabled,
                                    x_enabled: settingsPage.xEnabled,
                                    tiktok_enabled: settingsPage.tiktokEnabled,
                                    rate_limit_posts: settingsPage.rateLimitPosts,
                                    rate_limit_minutes: settingsPage.rateLimitMinutes
                                }
                                controller.saveSettings(JSON.stringify(cfg))
                            }
                        }
                    }
                }

                Rectangle {
                    width: resetLabel.implicitWidth + theme.spacingXxl
                    height: 40
                    radius: theme.radiusMd
                    color: theme.surfaceAlt

                    Text {
                        id: resetLabel
                        anchors.centerIn: parent
                        text: "Reset"
                        font.pixelSize: 13
                        color: theme.textSecondary
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            settingsPage.tiktokUsername = ""
                            settingsPage.downloadDir = ""
                            settingsPage.youtubeEnabled = true
                            settingsPage.instagramEnabled = true
                            settingsPage.xEnabled = true
                            settingsPage.tiktokEnabled = true
                            settingsPage.rateLimitPosts = 10
                            settingsPage.rateLimitMinutes = 60
                        }
                    }
                }

                Item { Layout.fillWidth: true }
            }
        }
    }
}

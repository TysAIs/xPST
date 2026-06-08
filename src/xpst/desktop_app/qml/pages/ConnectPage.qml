import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: connectPage
    background: Rectangle { color: theme.canvas }

    property var healthData: {
        try {
            if (typeof controller !== "undefined" && controller.platformHealth)
                return JSON.parse(controller.platformHealth)
        } catch(e) {}
        return ({})
    }
    property var connectingPlatforms: ({})

    // Refresh health when data changes
    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onDataChanged() {
            try {
                connectPage.healthData = JSON.parse(controller.platformHealth)
            } catch(e) {}
        }
    }

    function getPlatformStatus(platformName) {
        var key = platformName.toLowerCase()
        if (key === "x") key = "x"
        var info = healthData[key]
        if (!info) return { status: "unknown", connected: false, enabled: true }
        return {
            status: info.status || "unknown",
            connected: info.status === "ok" || info.status === "healthy" || info.status === "connected",
            enabled: info.enabled !== false,
            failures: info.failures || 0,
            canUpload: info.can_upload !== false,
            circuitBreakerOpen: info.circuit_breaker_open || false,
            lastSuccess: info.last_success || null
        }
    }

    function getHealthColor(status) {
        if (status === "ok" || status === "healthy" || status === "connected") return theme.success
        if (status === "warning" || status === "degraded") return theme.warning
        if (status === "error" || status === "failed") return theme.error
        return theme.textMuted
    }

    function getHealthLabel(status) {
        if (status === "ok" || status === "healthy" || status === "connected") return "Healthy"
        if (status === "warning" || status === "degraded") return "Degraded"
        if (status === "error" || status === "failed") return "Error"
        return "Unknown"
    }

    function connectPlatform(platformName) {
        if (typeof controller === "undefined") return
        connectPage.connectingPlatforms[platformName.toLowerCase()] = true
        connectPage.connectingPlatformsChanged()
        controller.connectPlatformAsync(platformName.toLowerCase())
    }

    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onConnectResult(jsonStr) {
            try {
                var result = JSON.parse(jsonStr)
                var key = result.platform || ""
                connectPage.connectingPlatforms[key] = false
                connectPage.connectingPlatformsChanged()
            } catch(e) {
                connectPage.connectingPlatforms = ({})
            }
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
                        { name: "YouTube",   key: "youtube",   icon: "▶️", color: theme.youtube },
                        { name: "Instagram", key: "instagram", icon: "📷", color: theme.instagram },
                        { name: "X",         key: "x",         icon: "𝕏",  color: theme.xtwitter },
                        { name: "TikTok",    key: "tiktok",    icon: "🎵", color: theme.tiktok }
                    ]

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 200
                        radius: theme.radiusXl
                        color: theme.surfaceCard

                        property var platformStatus: connectPage.getPlatformStatus(modelData.key)
                        property bool isConnecting: connectPage.connectingPlatforms[modelData.key] === true

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
                                            color: platformStatus.connected ? theme.success : theme.textMuted
                                        }
                                        Text {
                                            text: platformStatus.enabled ? (platformStatus.connected ? "Connected" : "Not connected") : "Disabled"
                                            font.pixelSize: 12
                                            color: theme.textSecondary
                                        }
                                    }
                                }
                                Item { Layout.fillWidth: true }
                            }

                            // Health status
                            RowLayout {
                                spacing: theme.spacingSm
                                Text {
                                    text: "Health:"
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    color: connectPage.getHealthColor(platformStatus.status)
                                }
                                Text {
                                    text: connectPage.getHealthLabel(platformStatus.status)
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                }

                                // Show failure count if any
                                Text {
                                    text: platformStatus.failures > 0 ? "(" + platformStatus.failures + " failures)" : ""
                                    font.pixelSize: 11
                                    color: theme.error
                                    visible: platformStatus.failures > 0
                                }

                                // Circuit breaker warning
                                Text {
                                    text: "⚠ Circuit breaker open"
                                    font.pixelSize: 11
                                    color: theme.warning
                                    visible: platformStatus.circuitBreakerOpen
                                }

                                Item { Layout.fillWidth: true }
                            }

                            // Quota info
                            RowLayout {
                                spacing: theme.spacingSm
                                Text {
                                    text: "Can upload:"
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                                Text {
                                    text: platformStatus.canUpload ? "Yes" : "No (quota reached)"
                                    font.pixelSize: 12
                                    color: platformStatus.canUpload ? theme.success : theme.warning
                                }
                                Item { Layout.fillWidth: true }
                            }

                            Item { Layout.fillHeight: true }

                            // Connect/Disconnect button
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: isConnecting ? theme.surfaceAlt
                                       : platformStatus.connected ? theme.surfaceAlt : theme.accent

                                RowLayout {
                                    anchors.centerIn: parent
                                    spacing: theme.spacingSm

                                    // Loading spinner
                                    BusyIndicator {
                                        width: 16; height: 16
                                        running: isConnecting
                                        visible: isConnecting
                                    }

                                    Text {
                                        text: isConnecting ? "Connecting..."
                                             : platformStatus.connected ? "Disconnect" : "Connect"
                                        font.pixelSize: 13
                                        font.bold: true
                                        color: platformStatus.connected ? theme.textSecondary : "#ffffff"
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    enabled: !isConnecting
                                    onClicked: connectPage.connectPlatform(modelData.name)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

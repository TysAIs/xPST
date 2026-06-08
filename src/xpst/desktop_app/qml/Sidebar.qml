import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: sidebar
    width: expanded ? 232 : 72
    Layout.fillHeight: true
    color: theme.surface
    border.color: theme.separator
    border.width: 1

    Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

    property bool expanded: true
    property string currentPage: "dashboard"
    property int notifCount: typeof notifModel !== "undefined" ? notifModel.rowCount() : 0
    signal navigate(string pageName)

    Connections {
        target: typeof notifModel !== "undefined" ? notifModel : null
        function onRowsInserted() { sidebar.notifCount = notifModel.rowCount() }
        function onModelReset() { sidebar.notifCount = notifModel.rowCount() }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: 18
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        anchors.bottomMargin: 14
        spacing: 6

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 48
            spacing: 10

            Rectangle {
                Layout.preferredWidth: 34
                Layout.preferredHeight: 34
                radius: 9
                color: theme.accent

                Text {
                    anchors.centerIn: parent
                    text: "x"
                    font.family: theme.fontFamily
                    font.pixelSize: 18
                    font.bold: true
                    color: "white"
                }
            }

            ColumnLayout {
                visible: sidebar.expanded
                Layout.fillWidth: true
                spacing: 0

                Text {
                    Layout.fillWidth: true
                    text: "xPST"
                    font.family: theme.fontFamily
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                }
                Text {
                    Layout.fillWidth: true
                    text: "Cross-posting studio"
                    font.family: theme.fontFamily
                    font.pixelSize: 11
                    color: theme.textMuted
                    elide: Text.ElideRight
                }
            }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: theme.separator; opacity: 0.8 }
        Item { Layout.preferredHeight: 8 }

        Repeater {
            model: [
                { label: "Dashboard", icon: "D", page: "dashboard" },
                { label: "Content",   icon: "C", page: "content" },
                { label: "Analytics", icon: "A", page: "analytics" },
                { label: "Connect",   icon: "L", page: "connect" },
                { label: "Schedule",  icon: "S", page: "schedule" },
                { label: "Settings",  icon: "G", page: "settings" },
                { label: "About",     icon: "I", page: "about" }
            ]

            Rectangle {
                id: navItem
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                radius: theme.radiusMd
                color: sidebar.currentPage === modelData.page
                       ? theme.accentMuted
                       : (navMouse.containsMouse ? theme.surfaceAlt : "transparent")

                Rectangle {
                    width: 3
                    height: 18
                    radius: 2
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    color: theme.accent
                    visible: sidebar.currentPage === modelData.page
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    anchors.rightMargin: 10
                    spacing: 10

                    Rectangle {
                        Layout.preferredWidth: 24
                        Layout.preferredHeight: 24
                        radius: 7
                        color: sidebar.currentPage === modelData.page ? theme.accent : theme.elevated
                        border.color: sidebar.currentPage === modelData.page ? theme.accent : theme.separator

                        Text {
                            anchors.centerIn: parent
                            text: modelData.icon
                            font.family: theme.fontFamily
                            font.pixelSize: 11
                            font.weight: Font.DemiBold
                            color: sidebar.currentPage === modelData.page ? "white" : theme.textSecondary
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: modelData.label
                        font.family: theme.fontFamily
                        font.pixelSize: theme.fontMd
                        font.weight: sidebar.currentPage === modelData.page ? Font.DemiBold : Font.Normal
                        color: sidebar.currentPage === modelData.page ? theme.textPrimary : theme.textSecondary
                        visible: sidebar.expanded
                        elide: Text.ElideRight
                    }
                }

                MouseArea {
                    id: navMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: sidebar.navigate(modelData.page)
                }
            }
        }

        Item { Layout.preferredHeight: 10 }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: sidebar.expanded ? 82 : 0
            visible: sidebar.expanded
            radius: theme.radiusXl
            color: theme.elevated
            border.color: theme.separator
            clip: true

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                spacing: 6

                Text {
                    text: "Today"
                    font.family: theme.fontFamily
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    color: theme.textMuted
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8

                    Text {
                        text: (typeof controller !== "undefined" ? controller.totalPosts : 0) + " posts"
                        font.family: theme.fontFamily
                        font.pixelSize: 13
                        color: theme.textPrimary
                    }

                    Item { Layout.fillWidth: true }

                    Rectangle {
                        width: 10
                        height: 10
                        radius: 5
                        color: {
                            try {
                                if (typeof controller !== "undefined") {
                                    var h = JSON.parse(controller.platformHealth)
                                    var healthy = 0, total = 0
                                    for (var k in h) {
                                        total++
                                        var s = h[k].status || "unknown"
                                        if (s === "ok" || s === "healthy" || s === "connected") healthy++
                                    }
                                    if (total === 0) return theme.textMuted
                                    if (healthy === total) return theme.success
                                    if (healthy > 0) return theme.warning
                                    return theme.error
                                }
                            } catch(e) {}
                            return theme.textMuted
                        }
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: "Local-first. No cloud service."
                    font.family: theme.fontFamily
                    font.pixelSize: 11
                    color: theme.textMuted
                    elide: Text.ElideRight
                }
            }
        }

        Item { Layout.fillHeight: true }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 38
            radius: theme.radiusMd
            color: bellMouse.containsMouse ? theme.surfaceAlt : "transparent"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 10
                spacing: 10

                Rectangle {
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 24
                    radius: 7
                    color: theme.elevated
                    border.color: theme.separator

                    Text {
                        anchors.centerIn: parent
                        text: "N"
                        font.family: theme.fontFamily
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        color: theme.textSecondary
                    }

                    Rectangle {
                        width: Math.max(14, badgeText.implicitWidth + 6)
                        height: 14
                        radius: 7
                        color: theme.error
                        visible: sidebar.notifCount > 0
                        anchors.top: parent.top
                        anchors.right: parent.right
                        anchors.topMargin: -5
                        anchors.rightMargin: -6

                        Text {
                            id: badgeText
                            anchors.centerIn: parent
                            text: sidebar.notifCount > 99 ? "99+" : String(sidebar.notifCount)
                            font.family: theme.fontFamily
                            font.pixelSize: 8
                            font.bold: true
                            color: "white"
                        }
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: "Notifications"
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontMd
                    color: theme.textSecondary
                    visible: sidebar.expanded
                    elide: Text.ElideRight
                }
            }

            MouseArea {
                id: bellMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: notifPopup.visible ? notifPopup.close() : notifPopup.open()
            }

            Popup {
                id: notifPopup
                y: -notifPopupContent.implicitHeight - 8
                x: sidebar.expanded ? 0 : 64
                width: 300
                height: Math.min(360, notifPopupContent.implicitHeight + 24)
                background: Rectangle {
                    color: theme.elevated
                    radius: theme.radiusXl
                    border.color: theme.separator
                    border.width: 1
                }

                ColumnLayout {
                    id: notifPopupContent
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 10

                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: "Notifications"
                            font.family: theme.fontFamily
                            font.pixelSize: 14
                            font.weight: Font.DemiBold
                            color: theme.textPrimary
                            Layout.fillWidth: true
                        }
                        Text {
                            text: "Clear"
                            font.family: theme.fontFamily
                            font.pixelSize: 12
                            color: theme.accent
                            visible: sidebar.notifCount > 0
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (typeof notifModel !== "undefined") notifModel.clear()
                                }
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: theme.separator }

                    ListView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: typeof notifModel !== "undefined" ? notifModel : 0

                        delegate: Rectangle {
                            width: parent ? parent.width : 280
                            height: notifDelegateCol.implicitHeight + 10
                            color: "transparent"

                            ColumnLayout {
                                id: notifDelegateCol
                                anchors.fill: parent
                                spacing: 3

                                Text {
                                    text: model.message || ""
                                    font.family: theme.fontFamily
                                    font.pixelSize: 12
                                    color: theme.textPrimary
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    maximumLineCount: 2
                                    elide: Text.ElideRight
                                }
                                Text {
                                    text: {
                                        if (!model.timestamp) return ""
                                        try {
                                            var d = new Date(model.timestamp)
                                            var now = new Date()
                                            var diff = (now - d) / 1000
                                            if (diff < 60) return "just now"
                                            if (diff < 3600) return Math.floor(diff / 60) + "m ago"
                                            if (diff < 86400) return Math.floor(diff / 3600) + "h ago"
                                            return d.toLocaleDateString()
                                        } catch(e) { return "" }
                                    }
                                    font.family: theme.fontFamily
                                    font.pixelSize: 10
                                    color: theme.textMuted
                                }
                            }
                        }

                        Text {
                            anchors.centerIn: parent
                            text: "No notifications yet"
                            font.family: theme.fontFamily
                            font.pixelSize: 12
                            color: theme.textMuted
                            visible: typeof notifModel !== "undefined" && notifModel.rowCount() === 0
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 38
            radius: theme.radiusMd
            color: themeMouse.containsMouse ? theme.surfaceAlt : "transparent"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 10
                spacing: 10

                Rectangle {
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 24
                    radius: 7
                    color: theme.elevated
                    border.color: theme.separator

                    Text {
                        anchors.centerIn: parent
                        text: theme.darkMode ? "D" : "L"
                        font.family: theme.fontFamily
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        color: theme.textSecondary
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: theme.darkMode ? "Dark" : "Light"
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontMd
                    color: theme.textSecondary
                    visible: sidebar.expanded
                }
            }

            MouseArea {
                id: themeMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: theme.darkMode = !theme.darkMode
            }
        }

        Text {
            Layout.fillWidth: true
            Layout.preferredHeight: 24
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            text: "v0.1.0"
            font.family: theme.fontFamily
            font.pixelSize: 11
            color: theme.textMuted
            visible: sidebar.expanded
        }
    }
}

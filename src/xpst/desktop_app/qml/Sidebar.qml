import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Rectangle {
    id: sidebar
    width: expanded ? 240 : 64
    Layout.fillHeight: true
    color: theme.surface
    Behavior on width { NumberAnimation { duration: 200; easing.type: Easing.InOutCubic } }

    property bool expanded: true
    property string currentPage: "dashboard"
    signal navigate(string pageName)

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Logo
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 64
            color: "transparent"

            RowLayout {
                anchors.centerIn: parent
                spacing: theme.spacingSm

                Text {
                    text: "⚡"
                    font.pixelSize: 18
                }
                Text {
                    text: "xPST"
                    font.pixelSize: 18
                    font.bold: true
                    color: theme.textPrimary
                    visible: sidebar.expanded
                }
            }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: theme.surfaceAlt }

        // Nav items
        Item { Layout.preferredHeight: theme.spacingXl }

        Repeater {
            model: [
                { label: "Dashboard", icon: "📊", page: "dashboard" },
                { label: "Content",   icon: "📝", page: "content" },
                { label: "Analytics", icon: "📈", page: "analytics" },
                { label: "Connect",   icon: "🔗", page: "connect" },
                { label: "Settings",  icon: "⚙", page: "settings" }
            ]

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                Layout.leftMargin: theme.spacingSm
                Layout.rightMargin: theme.spacingSm
                radius: theme.radiusMd
                color: navMouse.containsMouse || sidebar.currentPage === modelData.page
                       ? (sidebar.currentPage === modelData.page ? theme.accentMuted : theme.surfaceAlt)
                       : "transparent"

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: theme.spacingMd
                    spacing: theme.spacingMd

                    Text {
                        text: modelData.icon
                        font.pixelSize: 14
                        Layout.preferredWidth: 24
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Text {
                        text: modelData.label
                        font.pixelSize: 13
                        color: sidebar.currentPage === modelData.page ? theme.accent : theme.textSecondary
                        visible: sidebar.expanded
                        font.weight: sidebar.currentPage === modelData.page ? Font.DemiBold : Font.Normal
                    }
                    Item { Layout.fillWidth: true }
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

        Item { Layout.fillHeight: true }

        // Theme toggle
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            Layout.leftMargin: theme.spacingSm
            Layout.rightMargin: theme.spacingSm
            Layout.bottomMargin: theme.spacingSm
            radius: theme.radiusMd
            color: themeMouse.containsMouse ? theme.surfaceAlt : "transparent"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: theme.spacingMd
                spacing: theme.spacingMd

                Text {
                    text: theme.darkMode ? "🌙" : "☀️"
                    font.pixelSize: 14
                    Layout.preferredWidth: 24
                    horizontalAlignment: Text.AlignHCenter
                }
                Text {
                    text: theme.darkMode ? "Dark Mode" : "Light Mode"
                    font.pixelSize: 13
                    color: theme.textSecondary
                    visible: sidebar.expanded
                }
                Item { Layout.fillWidth: true }
            }

            MouseArea {
                id: themeMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: theme.darkMode = !theme.darkMode
            }
        }

        // Version
        Text {
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            text: "v1.0.0"
            font.pixelSize: 11
            color: theme.textMuted
            visible: sidebar.expanded
        }
    }
}

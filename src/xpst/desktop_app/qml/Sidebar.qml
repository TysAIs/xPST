import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import xpst.desktop_app.qml 1.0


Rectangle {
    id: sidebar
    // macOS unified titlebar: the native traffic lights occupy the top-left
    // window area; QML content that would collide with them must pad for it.
    // Default false so every other platform renders identically as before.
    property bool macosTrafficLightZone: typeof macUnifiedTitlebar !== "undefined"
                                         ? macUnifiedTitlebar
                                         : false
    width: expanded ? 240 : 64
    Layout.fillHeight: true
    color: theme.surface
    Behavior on width { NumberAnimation { duration: 200; easing.type: Easing.InOutCubic } }

    property bool expanded: true
    property string currentPage: "dashboard"
    signal navigate(string pageName)

    property int notifCount: typeof notifModel !== "undefined" ? notifModel.rowCount() : 0

    // Phase 3.1: spring-driven selection indicator (spring 1.5, damping 0.4,
    // mass 0.5). The active nav delegate updates `selectionIndicatorY` to its
    // vertical center (mapped into sidebar coords); the accent bar springs to
    // that position when the page changes.
    property real selectionIndicatorY: 0

    // Update count when model changes
    Connections {
        target: typeof notifModel !== "undefined" ? notifModel : null
        function onRowsInserted() { sidebar.notifCount = notifModel.rowCount() }
        function onModelReset() { sidebar.notifCount = notifModel.rowCount() }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Logo — big mark, top-left (image carries the brand; no text beside it)
        Rectangle {
            Layout.fillWidth: true
            // macOS (unified hidden titlebar) reserves the top window area for
            // the OS traffic lights — give the logo a taller hit zone so the
            // brand mark stays centered between them and the content edge.
            Layout.preferredHeight: sidebar.macosTrafficLightZone ? 76 : 72
            color: "transparent"

            Image {
                id: sidebarLogo
                source: typeof logoUrl !== "undefined" ? logoUrl : ""
                sourceSize: Qt.size(96, 96)
                fillMode: Image.PreserveAspectFit
                anchors.left: parent.left
                anchors.leftMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                Layout.preferredWidth: 52
                Layout.preferredHeight: 52
                visible: true
            }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: theme.surfaceAlt }

        // Nav items
        Item { Layout.preferredHeight: theme.spacingXl }

        Repeater {
            // Icons routed through the bundled Lucide icon font (W4-3/W4-5).
            // The Analytics entry previously used a corrupted U+FFFD glyph; all
            // nav icons now use real font glyphs so none render as tofu boxes.
            model: [
                { label: "Dashboard", icon: Icons.dashboard, page: "dashboard" },
                { label: "Compose",   icon: Icons.edit,      page: "compose" },
                { label: "Library",   icon: Icons.content,   page: "content" },
                { label: "Analytics", icon: Icons.analytics, page: "analytics" },
                { label: "Accounts",  icon: Icons.connect,   page: "connect" },
                { label: "Automations", icon: Icons.schedule,  page: "schedule" },
                { label: "Settings",  icon: Icons.settings,  page: "settings" },
                { label: "About",     icon: Icons.about,     page: "about" }
            ]

            Rectangle {
                id: navItem
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                Layout.leftMargin: theme.spacingSm
                Layout.rightMargin: theme.spacingSm
                radius: theme.radiusMd
                color: {
                    if (sidebar.currentPage === modelData.page)
                        return theme.accentMuted
                    if (navMouse.containsMouse)
                        return theme.surfaceAlt
                    return "transparent"
                }
                Behavior on color { ColorAnimation { duration: 120; easing.type: Easing.OutCubic } }

                // Phase 3.2: hover/press micro-interaction on nav items.
                Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
                scale: navMouse.containsPress ? 0.98 : (navMouse.containsMouse ? 1.02 : 1.0)

                // Phase 3.1: report this item's vertical center so the
                // selection accent bar can spring to it.
                Component.onCompleted: navItem.updateIndicator()
                onYChanged: navItem.updateIndicator()
                function updateIndicator() {
                    if (sidebar.currentPage === modelData.page)
                        sidebar.selectionIndicatorY = navItem.mapToItem(sidebar, 0, navItem.height / 2).y
                }
                Connections {
                    target: sidebar
                    function onCurrentPageChanged() { navItem.updateIndicator() }
                    function onWidthChanged() { navItem.updateIndicator() }
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: theme.spacingMd
                    spacing: theme.spacingMd

                    Text {
                        text: modelData.icon
                        font.family: theme.iconFontFamily
                        font.pixelSize: 14
                        Layout.preferredWidth: 24
                        horizontalAlignment: Text.AlignHCenter
                        color: sidebar.currentPage === modelData.page ? theme.accent : theme.textSecondary
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
                    propagateComposedEvents: true
                    onClicked: sidebar.navigate(modelData.page)
                }
            }
        }

        // Quick Stats section
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: sidebar.expanded ? quickStatsCol.implicitHeight + theme.spacingLg : 0
            Layout.leftMargin: theme.spacingSm
            Layout.rightMargin: theme.spacingSm
            Layout.bottomMargin: theme.spacingSm
            radius: theme.radiusMd
            color: theme.surfaceAlt
            visible: sidebar.expanded
            clip: true

            ColumnLayout {
                id: quickStatsCol
                anchors.fill: parent
                anchors.margins: theme.spacingSm
                spacing: theme.spacingXs

                Text {
                    text: "Quick Stats"
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                    color: theme.textMuted
                }

                RowLayout {
                    spacing: theme.spacingSm

                    Text {
                        text: theme.iconStats
                        font.family: theme.iconFontFamily
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        color: theme.accent
                    }
                    Text {
                        id: totalPostsText
                        text: (typeof controller !== "undefined" ? controller.totalPosts : 0) + " posts"
                        font.pixelSize: 11
                        color: theme.textSecondary
                        Behavior on text {
                            SequentialAnimation {
                                NumberAnimation { target: totalPostsText; property: "scale"; from: 1.0; to: 1.15; duration: 150; easing.type: Easing.OutCubic }
                                NumberAnimation { target: totalPostsText; property: "scale"; from: 1.15; to: 1.0; duration: 150; easing.type: Easing.InOutCubic }
                            }
                        }
                    }

                    Item { Layout.fillWidth: true }

                    // Health dot
                    Rectangle {
                        width: 10; height: 10; radius: 5
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
                            } catch(e) { console.error("Sidebar health dot parse failed", e) }
                            return theme.textMuted
                        }
                    }
                }
            }
        }

        Item { Layout.fillHeight: true }

        // Notification bell (Item 8)
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            Layout.leftMargin: theme.spacingSm
            Layout.rightMargin: theme.spacingSm
            Layout.bottomMargin: theme.spacingSm
            radius: theme.radiusMd
            color: bellMouse.containsMouse ? theme.surfaceAlt : "transparent"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: theme.spacingMd
                spacing: theme.spacingMd

                // Bell icon with badge
                Item {
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 24

                    Text {
                        text: theme.iconBell
                        font.family: theme.iconFontFamily
                        font.pixelSize: 14
                        color: theme.textSecondary
                        anchors.centerIn: parent
                    }

                    // Badge count
                    Rectangle {
                        width: Math.max(16, badgeText.implicitWidth + 6)
                        height: 16
                        radius: 8
                        color: theme.error
                        visible: sidebar.notifCount > 0
                        anchors.top: parent.top
                        anchors.right: parent.right
                        anchors.topMargin: -4
                        anchors.rightMargin: -6

                        Text {
                            id: badgeText
                            anchors.centerIn: parent
                            text: sidebar.notifCount > 99 ? "99+" : String(sidebar.notifCount)
                            font.pixelSize: 9
                            font.weight: Font.DemiBold
                            color: "#ffffff"
                        }
                    }
                }

                Text {
                    text: "Notifications"
                    font.pixelSize: 13
                    color: theme.textSecondary
                    visible: sidebar.expanded
                }
                Item { Layout.fillWidth: true }
            }

            MouseArea {
                id: bellMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    // Show notification popup
                    if (notifPopup.visible) {
                        notifPopup.close()
                    } else {
                        notifPopup.open()
                    }
                }
            }

            // Notification popup
            Popup {
                id: notifPopup
                y: -notifPopupContent.implicitHeight - 8
                x: sidebar.expanded ? 0 : 64
                width: 280
                height: Math.min(360, notifPopupContent.implicitHeight + theme.spacingXl)
                background: Rectangle {
                    color: theme.surfaceCard
                    radius: theme.radiusLg
                    border.color: theme.surfaceAlt
                    border.width: 1
                }

                ColumnLayout {
                    id: notifPopupContent
                    anchors.fill: parent
                    anchors.margins: theme.spacingMd
                    spacing: theme.spacingSm

                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: "Notifications"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            color: theme.textPrimary
                            Layout.fillWidth: true
                        }
                        Rectangle {
                            width: clearLabel.implicitWidth + theme.spacingMd
                            height: 24
                            radius: theme.radiusSm
                            color: theme.surfaceAlt
                            visible: sidebar.notifCount > 0
                            Text {
                                id: clearLabel
                                anchors.centerIn: parent
                                text: "Clear"
                                font.pixelSize: 10
                                color: theme.textSecondary
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (typeof notifModel !== "undefined") notifModel.clear()
                                }
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: theme.surfaceAlt }

                    // Notification list
                    ListView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: typeof notifModel !== "undefined" ? notifModel : 0

                        delegate: Rectangle {
                            width: parent ? parent.width : 280
                            height: notifDelegateCol.implicitHeight + theme.spacingMd
                            color: "transparent"

                            ColumnLayout {
                                id: notifDelegateCol
                                anchors.fill: parent
                                spacing: 2

                                RowLayout {
                                    spacing: theme.spacingXs
                                    Text {
                                        text: model.isError ? Icons.error : Icons.check
                                        font.family: theme.iconFontFamily
                                        font.pixelSize: 10
                                    }
                                    Text {
                                        text: model.message || ""
                                        font.pixelSize: 11
                                        color: theme.textPrimary
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                    }
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
                                        } catch(e) { console.error("Sidebar timestamp parse failed", e); return "" }
                                    }
                                    font.pixelSize: 9
                                    color: theme.textMuted
                                }
                            }
                        }

                        // Empty state
                        Text {
                            anchors.centerIn: parent
                            text: "No notifications yet"
                            font.pixelSize: 11
                            color: theme.textMuted
                            visible: typeof notifModel !== "undefined" && notifModel.rowCount() === 0
                        }
                    }
                }
            }
        }

        // Theme toggle
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            Layout.leftMargin: theme.spacingSm
            Layout.rightMargin: theme.spacingSm
            Layout.bottomMargin: theme.spacingSm
            radius: theme.radiusMd
            color: themeSwitchHover.hovered ? theme.surfaceAlt : "transparent"
            HoverHandler { id: themeSwitchHover }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: theme.spacingMd
                spacing: theme.spacingMd

                // Sun (light) / Moon (dark) with a proper toggle switch.
                Text {
                    text: theme.darkMode ? theme.iconMoon : theme.iconSun
                    font.family: theme.iconFontFamily
                    font.pixelSize: 14
                    color: theme.textSecondary
                    Layout.preferredWidth: 24
                    horizontalAlignment: Text.AlignHCenter
                }
                Text {
                    text: "Appearance"
                    font.pixelSize: 13
                    color: theme.textSecondary
                    visible: sidebar.expanded
                }
                Item { Layout.fillWidth: true }
                Switch {
                    id: themeSwitch
                    visible: sidebar.expanded
                    checked: theme.darkMode
                    Accessible.role: Accessible.CheckBox
                    Accessible.name: "Dark mode"
                    indicator: Rectangle {
                        implicitWidth: 34; implicitHeight: 18
                        radius: 9
                        color: themeSwitch.checked ? theme.accent : theme.textMuted
                        Behavior on color { ColorAnimation { duration: 150 } }
                        Rectangle {
                            x: themeSwitch.checked ? parent.width - width - 2 : 2
                            anchors.verticalCenter: parent.verticalCenter
                            width: 14; height: 14; radius: 7
                            color: "#ffffff"
                            Behavior on x { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
                        }
                    }
                    onToggled: theme.darkMode = checked
                }
                // Compact icon-only toggle when the sidebar is collapsed.
                MouseArea {
                    visible: !sidebar.expanded
                    anchors.fill: undefined
                    Layout.preferredWidth: 24
                    cursorShape: Qt.PointingHandCursor
                    onClicked: theme.darkMode = !theme.darkMode
                }
            }
        }

        // Version
        Text {
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            text: "v" + (typeof controller !== "undefined" ? controller.appVersion : "1.0.0")
            font.pixelSize: 11
            color: theme.textMuted
            visible: sidebar.expanded
        }
    }

    // Phase 3.1: spring-driven selection accent bar. Painted above the
    // ColumnLayout so it sits on the left edge of the active nav item.
    Rectangle {
        id: selectionIndicator
        x: 0
        width: 3
        height: 28
        radius: 2
        color: theme.accent
        visible: sidebar.selectionIndicatorY > 0
        y: sidebar.selectionIndicatorY - height / 2
        Behavior on y {
            SpringAnimation {
                spring: 1.5
                damping: 0.4
                mass: 0.5
            }
        }
    }
}

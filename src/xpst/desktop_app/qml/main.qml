import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


ApplicationWindow {
    id: root
    visible: true
    width: 1280
    height: 800
    minimumWidth: 960
    minimumHeight: 600
    title: "xPST — Cross-Posting Suite"
    color: theme.canvas

    property string currentPage: "dashboard"
    property var dialogStack: []

    // ── Toast notification ───────────────────────────────────────
    Rectangle {
        id: toast
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 32
        width: toastText.implicitWidth + 48
        height: 44
        radius: theme.radiusMd
        color: toast.isError ? theme.error : theme.success
        opacity: 0
        z: 1000

        property bool isError: false
        property string message: ""

        Text {
            id: toastText
            anchors.centerIn: parent
            text: toast.message
            font.pixelSize: 13
            font.bold: true
            color: "#ffffff"
        }

        function show(msg, error) {
            toast.message = msg
            toast.isError = error || false
            toast.opacity = 1
            toastTimer.restart()
        }

        Timer {
            id: toastTimer
            interval: 3000
            onTriggered: toast.opacity = 0
        }

        Behavior on opacity { NumberAnimation { duration: 200 } }
    }

    // ── Navigation ──────────────────────────────────────────────
    function navigateTo(pageName) {
        currentPage = pageName
        var component
        switch (pageName) {
        case "dashboard":
            component = Qt.createComponent("pages/DashboardPage.qml")
            break
        case "content":
            component = Qt.createComponent("pages/ContentPage.qml")
            break
        case "analytics":
            component = Qt.createComponent("pages/AnalyticsPage.qml")
            break
        case "connect":
            component = Qt.createComponent("pages/ConnectPage.qml")
            break
        case "settings":
            component = Qt.createComponent("pages/SettingsPage.qml")
            break
        default:
            component = Qt.createComponent("pages/DashboardPage.qml")
        }
        if (component.status === Component.Ready) {
            stackView.replace(component)
        } else {
            console.log("Page load error:", component.errorString())
        }
    }

    function showToast(msg, isError) {
        toast.show(msg, isError)
    }

    // ── Keyboard Shortcuts ──────────────────────────────────────
    Shortcut { sequence: "Meta+1"; onActivated: navigateTo("dashboard") }
    Shortcut { sequence: "Meta+2"; onActivated: navigateTo("content") }
    Shortcut { sequence: "Meta+3"; onActivated: navigateTo("analytics") }
    Shortcut { sequence: "Meta+4"; onActivated: navigateTo("connect") }
    Shortcut { sequence: "Meta+5"; onActivated: navigateTo("settings") }
    Shortcut { sequence: "Ctrl+1"; onActivated: navigateTo("dashboard") }
    Shortcut { sequence: "Ctrl+2"; onActivated: navigateTo("content") }
    Shortcut { sequence: "Ctrl+3"; onActivated: navigateTo("analytics") }
    Shortcut { sequence: "Ctrl+4"; onActivated: navigateTo("connect") }
    Shortcut { sequence: "Ctrl+5"; onActivated: navigateTo("settings") }
    Shortcut { sequence: "Meta+R"; onActivated: controller.refreshData() }
    Shortcut { sequence: "Ctrl+R"; onActivated: controller.refreshData() }
    Shortcut { sequence: "Meta+Q"; onActivated: Qt.quit() }
    Shortcut {
        sequence: "Escape"
        onActivated: {
            // Close any active dialog/popup
            if (stackView.currentItem && stackView.currentItem.closeDialog)
                stackView.currentItem.closeDialog()
        }
    }

    // ── Controller signal connections ───────────────────────────
    Connections {
        target: typeof controller !== "undefined" ? controller : null

        function onError(msg) {
            showToast(msg, true)
        }

        function onSettingsSaved(ok, msg) {
            showToast(ok ? msg : ("Save failed: " + msg), !ok)
        }

        function onConnectResult(jsonStr) {
            var result = JSON.parse(jsonStr)
            if (result.ok) {
                showToast(result.platform + " connected successfully", false)
            } else {
                showToast("Connection failed: " + (result.error || "Unknown error"), true)
            }
            // Trigger page refresh
            controller.refreshData()
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Sidebar {
            id: sidebar
            currentPage: root.currentPage
            onNavigate: function (pageName) {
                root.navigateTo(pageName)
            }
        }

        StackView {
            id: stackView
            Layout.fillWidth: true
            Layout.fillHeight: true
            initialItem: "pages/DashboardPage.qml"

            pushEnter: Transition {
                ParallelAnimation {
                    PropertyAnimation { property: "opacity"; from: 0; to: 1; duration: 200 }
                    PropertyAnimation { property: "x"; from: 40; to: 0; duration: 200; easing.type: Easing.OutCubic }
                }
            }
            pushExit: Transition {
                PropertyAnimation { property: "opacity"; from: 1; to: 0; duration: 150 }
            }
            popEnter: Transition {
                PropertyAnimation { property: "opacity"; from: 0; to: 1; duration: 200 }
            }
            popExit: Transition {
                ParallelAnimation {
                    PropertyAnimation { property: "opacity"; from: 1; to: 0; duration: 150 }
                    PropertyAnimation { property: "x"; from: 0; to: 40; duration: 150 }
                }
            }
            replaceEnter: Transition {
                ParallelAnimation {
                    PropertyAnimation { property: "opacity"; from: 0; to: 1; duration: 200 }
                    PropertyAnimation { property: "x"; from: 30; to: 0; duration: 200; easing.type: Easing.OutCubic }
                }
            }
            replaceExit: Transition {
                PropertyAnimation { property: "opacity"; from: 1; to: 0; duration: 150 }
            }
        }
    }
}

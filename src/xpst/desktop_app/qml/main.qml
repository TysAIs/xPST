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

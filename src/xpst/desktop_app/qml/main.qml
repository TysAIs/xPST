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

    // ── Window State Persistence ───────────────────────────────────
    Component.onCompleted: {
        var settings = Qt.application.settings || null
        // Restore geometry from QSettings via controller or direct settings
        restoreWindowGeometry()
    }

    Component.onDestruction: {
        saveWindowGeometry()
    }

    function restoreWindowGeometry() {
        // Uses QSettings (organization/app name set in main.py)
        try {
            var s = root.settings
            if (s) {
                var x = s.value("window/x", -1)
                var y = s.value("window/y", -1)
                var w = s.value("window/width", 1280)
                var h = s.value("window/height", 800)
                if (x >= 0 && y >= 0) {
                    root.x = x
                    root.y = y
                }
                root.width = Math.max(w, root.minimumWidth)
                root.height = Math.max(h, root.minimumHeight)
            }
        } catch(e) { /* first launch or no settings */ }
    }

    function saveWindowGeometry() {
        try {
            var s = root.settings
            if (s) {
                s.setValue("window/x", root.x)
                s.setValue("window/y", root.y)
                s.setValue("window/width", root.width)
                s.setValue("window/height", root.height)
            }
        } catch(e) {}
    }

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

    // ── Crash Recovery Dialog ─────────────────────────────────────
    Dialog {
        id: crashRecoveryDialog
        anchors.centerIn: parent
        width: 400
        modal: true
        title: "Error"
        closePolicy: Popup.CloseOnEscape

        property string errorMessage: ""
        property string errorPlatform: ""

        contentItem: ColumnLayout {
            spacing: 16

            Text {
                text: "⚠️"
                font.pixelSize: 32
                Layout.alignment: Qt.AlignHCenter
            }

            Text {
                text: crashRecoveryDialog.errorMessage || "An error occurred. Would you like to retry?"
                font.pixelSize: 13
                color: theme.textPrimary
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
            }

            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 12

                Rectangle {
                    width: retryLabel.implicitWidth + 32
                    height: 36
                    radius: theme.radiusMd
                    color: theme.accent

                    Text {
                        id: retryLabel
                        anchors.centerIn: parent
                        text: "Retry"
                        font.pixelSize: 13
                        font.bold: true
                        color: "#ffffff"
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            crashRecoveryDialog.close()
                            if (crashRecoveryDialog.errorPlatform.length > 0) {
                                controller.connectPlatform(crashRecoveryDialog.errorPlatform)
                            } else {
                                controller.refreshData()
                            }
                        }
                        Accessible.name: "Retry the failed operation"
                        Accessible.role: Accessible.Button
                    }
                }

                Rectangle {
                    width: dismissLabel.implicitWidth + 32
                    height: 36
                    radius: theme.radiusMd
                    color: theme.surfaceAlt

                    Text {
                        id: dismissLabel
                        anchors.centerIn: parent
                        text: "Dismiss"
                        font.pixelSize: 13
                        color: theme.textSecondary
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: crashRecoveryDialog.close()
                        Accessible.name: "Dismiss error dialog"
                        Accessible.role: Accessible.Button
                    }
                }
            }
        }
    }

    // ── Upload Progress Overlay ───────────────────────────────────
    property var uploadProgresses: ({})

    Rectangle {
        id: progressOverlay
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: 240  // sidebar width
        height: progressCol.children.length > 0 ? progressCol.implicitHeight + 24 : 0
        color: theme.surface
        opacity: progressCol.children.length > 0 ? 1.0 : 0.0
        z: 900

        Behavior on opacity { NumberAnimation { duration: 200 } }
        Behavior on height { NumberAnimation { duration: 200 } }

        ColumnLayout {
            id: progressCol
            anchors.fill: parent
            anchors.margins: 12
            spacing: 6

            Repeater {
                model: Object.keys(root.uploadProgresses)

                RowLayout {
                    spacing: 8
                    visible: modelData !== ""

                    Text {
                        text: modelData.charAt(0).toUpperCase() + modelData.slice(1)
                        font.pixelSize: 12
                        color: theme.textSecondary
                        Layout.preferredWidth: 80
                    }

                    ProgressBar {
                        from: 0
                        to: 100
                        value: root.uploadProgresses[modelData] || 0
                        Layout.fillWidth: true
                        Accessible.name: modelData + " upload progress"
                        Accessible.role: Accessible.ProgressBar

                        background: Rectangle {
                            implicitHeight: 6
                            color: theme.surfaceAlt
                            radius: 3
                        }

                        contentItem: Item {
                            implicitHeight: 6
                            Rectangle {
                                width: parent.width * (progressBarVisual.parent ? progressBarVisual.parent.parent.parent.value / 100 : 0)
                                height: 6
                                radius: 3
                                color: theme.accent
                            }
                        }
                    }

                    Text {
                        text: Math.round(root.uploadProgresses[modelData] || 0) + "%"
                        font.pixelSize: 11
                        color: theme.textMuted
                        Layout.preferredWidth: 36
                    }
                }
            }
        }
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
        case "about":
            component = Qt.createComponent("pages/AboutPage.qml")
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
            if (crashRecoveryDialog.visible) {
                crashRecoveryDialog.close()
                return
            }
            if (stackView.currentItem && stackView.currentItem.closeDialog)
                stackView.currentItem.closeDialog()
        }
    }

    // ── Controller signal connections ───────────────────────────
    Connections {
        target: typeof controller !== "undefined" ? controller : null

        function onError(msg) {
            // Show crash recovery dialog for serious errors
            crashRecoveryDialog.errorMessage = msg
            crashRecoveryDialog.errorPlatform = ""
            crashRecoveryDialog.open()
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

        function onProgressChanged(platform, progress) {
            var p = Object.assign({}, root.uploadProgresses)
            p[platform] = progress
            root.uploadProgresses = p

            // Remove from progress map when complete
            if (progress >= 100) {
                var cleanupTimer = Qt.createQmlObject(
                    'import QtQuick 2.15; Timer { interval: 1500; running: true; repeat: false }',
                    root, "cleanupTimer"
                )
                cleanupTimer.triggered.connect(function() {
                    var p2 = Object.assign({}, root.uploadProgresses)
                    delete p2[platform]
                    root.uploadProgresses = p2
                    cleanupTimer.destroy()
                })
            }
        }
    }

    // ── Drop Area for video files (Item 7) ────────────────────────
    DropArea {
        id: dropArea
        anchors.fill: parent
        keys: ["text/uri-list"]

        property bool containsVideo: false

        onEntered: function(drag) {
            var urls = drag.urls || []
            containsVideo = urls.some(function(u) {
                return u.match(/\.(mp4|mov|avi|mkv|webm|flv|wmv|m4v)$/i)
            })
        }

        onDropped: function(drop) {
            containsVideo = false
            var urls = drop.urls || []
            var videoFiles = urls.filter(function(u) {
                return u.match(/\.(mp4|mov|avi|mkv|webm|flv|wmv|m4v)$/i)
            })
            if (videoFiles.length === 0) {
                showToast("No video files detected", true)
                return
            }
            // Process each dropped video file
            for (var i = 0; i < videoFiles.length; i++) {
                var filePath = videoFiles[i]
                // Remove file:// prefix if present
                if (filePath.startsWith("file://"))
                    filePath = filePath.substring(7)
                // Prompt for caption via a simple dialog
                dropCaptionDialog.droppedPath = filePath
                dropCaptionDialog.open()
            }
        }

        onExited: {
            containsVideo = false
        }
    }

    // Drop overlay visual feedback
    Rectangle {
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.6)
        visible: dropArea.containsVideo
        z: 999

        ColumnLayout {
            anchors.centerIn: parent
            spacing: 16

            Text {
                text: "📹"
                font.pixelSize: 48
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
            }
            Text {
                text: "Drop video to post"
                font.pixelSize: 18
                font.bold: true
                color: "#ffffff"
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
            }
        }
    }

    // Caption prompt dialog for dropped videos
    Dialog {
        id: dropCaptionDialog
        anchors.centerIn: parent
        width: 450
        modal: true
        title: "Post Video"
        closePolicy: Popup.CloseOnEscape

        property string droppedPath: ""

        contentItem: ColumnLayout {
            spacing: 16

            Text {
                text: "Drop Caption Dialog"
                font.pixelSize: 0
                visible: false
                Accessible.ignored: true
            }

            Text {
                text: "🎬 " + (dropCaptionDialog.droppedPath ? dropCaptionDialog.droppedPath.split("/").pop() : "")
                font.pixelSize: 14
                font.bold: true
                color: theme.textPrimary
                Layout.fillWidth: true
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 80
                radius: theme.radiusMd
                color: theme.surfaceAlt
                border.color: theme.textMuted
                border.width: 1

                Flickable {
                    anchors.fill: parent
                    anchors.margins: theme.spacingSm
                    clip: true
                    contentHeight: captionInput.implicitHeight

                    TextEdit {
                        id: captionInput
                        width: parent.width
                        color: theme.textPrimary
                        font.pixelSize: 12
                        wrapMode: TextEdit.Wrap
                        property string placeholderText: "Enter caption..."

                        Text {
                            anchors.fill: parent
                            text: captionInput.placeholderText
                            font: captionInput.font
                            color: theme.textMuted
                            visible: captionInput.text.length === 0
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 12

                Item { Layout.fillWidth: true }

                Rectangle {
                    width: cancelLabel.implicitWidth + 32
                    height: 36
                    radius: theme.radiusMd
                    color: theme.surfaceAlt
                    Text {
                        id: cancelLabel
                        anchors.centerIn: parent
                        text: "Cancel"
                        font.pixelSize: 13
                        color: theme.textSecondary
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            captionInput.text = ""
                            dropCaptionDialog.close()
                        }
                    }
                }

                Rectangle {
                    width: postLabel.implicitWidth + 32
                    height: 36
                    radius: theme.radiusMd
                    color: theme.accent
                    Text {
                        id: postLabel
                        anchors.centerIn: parent
                        text: "Post"
                        font.pixelSize: 13
                        font.bold: true
                        color: "#ffffff"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (dropCaptionDialog.droppedPath.length > 0 && typeof controller !== "undefined") {
                                controller.postVideo(dropCaptionDialog.droppedPath, captionInput.text)
                                showToast("Posting: " + dropCaptionDialog.droppedPath.split("/").pop(), false)
                            }
                            captionInput.text = ""
                            dropCaptionDialog.close()
                        }
                    }
                }
            }
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

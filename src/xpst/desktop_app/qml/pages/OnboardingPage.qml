import QtQuick 2.15
import xpst.desktop_app.qml 1.0
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Dialogs
import "../components"

// Phase 5.1 — First-run onboarding wizard.
//
// Shown as the StackView initial item when the persisted
// `first_run_complete` config flag is false (see main.qml checkFirstRun).
// Guides the user through four steps: welcome, pick content folder,
// connect a platform, and ready-to-post. On finish, calls
// controller.markOnboardingComplete() so the wizard never reappears.
Page {
    id: onboardingPage
    background: Rectangle { color: theme.canvas }

    function closeDialog() {}

    // ── Wizard state ──────────────────────────────────────────────
    property int currentStep: 0          // 0..3
    property string contentFolder: ""    // local source path chosen in step 1
    property string statusMessage: ""
    property bool saving: false

    readonly property var stepTitles: [
        "Welcome to xPST",
        "Pick your content folder",
        "Connect a platform",
        "Ready to post"
    ]

    Component.onCompleted: {
        // Pre-fill the content folder from existing config if present so a
        // returning-but-not-completed user doesn't lose their choice.
        if (typeof controller !== "undefined" && controller.configData) {
            try {
                var cfg = JSON.parse(controller.configData)
                if (cfg && cfg.local && cfg.local.path)
                    onboardingPage.contentFolder = cfg.local.path
            } catch(e) { console.warn('OnboardingPage: failed to read existing config', e) }
        }
    }

    // ── Folder picker (standard QtQuick.Dialogs FolderDialog) ─────
    FolderDialog {
        id: folderDialog
        title: "Choose content folder"
        onAccepted: {
            var url = String(folderDialog.selectedFolder)
            // Strip the file:// prefix (platform-aware like main.qml DropArea).
            if (url.startsWith("file://")) {
                url = url.substring(7)
                if (/^\/[A-Za-z]:/.test(url))
                    url = url.substring(1)
            }
            onboardingPage.contentFolder = url
        }
    }

    // ── Layout ────────────────────────────────────────────────────
    Flickable {
        anchors.fill: parent
        contentHeight: wizardCol.implicitHeight + theme.spacingXxl
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: wizardCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: theme.pageMargin
            spacing: theme.spacingXl

            // Step indicator
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: theme.spacingSm

                Repeater {
                    model: 4
                    Rectangle {
                        width: 28; height: 4; radius: 2
                        color: index <= onboardingPage.currentStep ? theme.accent : theme.surfaceAlt
                        Accessible.name: "Step " + (index + 1) + (index === onboardingPage.currentStep ? " (current)" : "")
                    }
                }
            }

            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Step " + (onboardingPage.currentStep + 1) + " of 4"
                font.pixelSize: 11
                color: theme.textMuted
            }

            Text {
                Layout.alignment: Qt.AlignHCenter
                text: onboardingPage.stepTitles[onboardingPage.currentStep]
                font.pixelSize: 24
                font.weight: Font.DemiBold
                color: theme.textPrimary
                Accessible.role: Accessible.Heading
            }

            // Card containing the step body
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: stepBody.implicitHeight + theme.spacingXxl
                radius: theme.radiusLg
                color: theme.surfaceCard

                ColumnLayout {
                    id: stepBody
                    anchors.fill: parent
                    anchors.margins: theme.pageMargin
                    spacing: theme.spacingLg

                    // ── Step 0: Welcome ─────────────────────────────
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingLg
                        visible: onboardingPage.currentStep === 0

                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            text: Icons.logo
                            font.family: theme.iconFontFamily
                            font.pixelSize: 56
                            color: theme.accent
                        }

                        Text {
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            text: "xPST — Cross-Posting Suite"
                            font.pixelSize: 18
                            font.weight: Font.DemiBold
                            color: theme.textPrimary
                        }

                        Text {
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            text: "Post one video to YouTube, Instagram, X, and TikTok at once. " +
                                  "This quick setup will get you ready to cross-post in under a minute."
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }
                    }

                    // ── Step 1: Pick content folder ─────────────────
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd
                        visible: onboardingPage.currentStep === 1

                        Text {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: "Choose the local folder where xPST should look for videos to cross-post. " +
                                  "You can change this later in Settings."
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingSm

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
                                    text: onboardingPage.contentFolder
                                    color: theme.textPrimary
                                    font.pixelSize: 13
                                    clip: true
                                    onTextChanged: onboardingPage.contentFolder = text
                                    Accessible.name: "Content folder path"
                                    Accessible.role: Accessible.EditableText
                                }
                            }

                            Rectangle {
                                width: browseLabel.implicitWidth + 28
                                height: 40
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1

                                Text {
                                    id: browseLabel
                                    anchors.centerIn: parent
                                    text: "Browse"
                                    font.pixelSize: 13
                                    color: theme.textPrimary
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: folderDialog.open()
                                    Accessible.name: "Browse for content folder"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: onboardingPage.contentFolder.length > 0
                                  ? "Selected: " + onboardingPage.contentFolder
                                  : "No folder selected — you can skip this and set it later."
                            font.pixelSize: 11
                            color: onboardingPage.contentFolder.length > 0 ? theme.success : theme.textMuted
                        }
                    }

                    // ── Step 2: Connect a platform ──────────────────
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd
                        visible: onboardingPage.currentStep === 2

                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            text: Icons.connect
                            font.family: theme.iconFontFamily
                            font.pixelSize: 40
                            color: theme.accent
                        }

                        Text {
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            text: "Connect your first platform so xPST can publish videos on your behalf. " +
                                  "You can connect more later from the Connect page."
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }

                        Rectangle {
                            Layout.alignment: Qt.AlignHCenter
                            width: openConnectLabel.implicitWidth + 40
                            height: 40
                            radius: theme.radiusMd
                            color: theme.accent

                            Text {
                                id: openConnectLabel
                                anchors.centerIn: parent
                                text: "Open Connect page"
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                color: "#ffffff"
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (typeof root !== "undefined" && root.navigateTo)
                                        root.navigateTo("connect")
                                }
                                Accessible.name: "Open the Connect page to add a platform"
                                Accessible.role: Accessible.Button
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            text: "You can finish this step now or come back to it later."
                            font.pixelSize: 11
                            color: theme.textMuted
                        }
                    }

                    // ── Step 3: Ready to post ───────────────────────
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd
                        visible: onboardingPage.currentStep === 3

                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            text: Icons.check
                            font.family: theme.iconFontFamily
                            font.pixelSize: 48
                            color: theme.success
                        }

                        Text {
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            text: "You're all set! Open the Compose page to select a video and cross-post it to every connected platform."
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }

                        Rectangle {
                            Layout.alignment: Qt.AlignHCenter
                            width: openComposeLabel.implicitWidth + 40
                            height: 40
                            radius: theme.radiusMd
                            color: theme.surfaceAlt
                            border.color: theme.textMuted
                            border.width: 1

                            Text {
                                id: openComposeLabel
                                anchors.centerIn: parent
                                text: "Open Compose page"
                                font.pixelSize: 13
                                color: theme.textPrimary
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (typeof root !== "undefined" && root.navigateTo)
                                        root.navigateTo("compose")
                                }
                                Accessible.name: "Open the Compose page to post a video"
                                Accessible.role: Accessible.Button
                            }
                        }
                    }

                    // Status / error line
                    Text {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        text: onboardingPage.statusMessage
                        font.pixelSize: 11
                        color: theme.error
                        visible: onboardingPage.statusMessage.length > 0
                    }
                }
            }

            // ── Nav buttons ─────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd

                // Back (hidden on first step)
                Rectangle {
                    visible: onboardingPage.currentStep > 0
                    width: backLabel.implicitWidth + 32
                    height: 40
                    radius: theme.radiusMd
                    color: theme.surfaceAlt

                    Text {
                        id: backLabel
                        anchors.centerIn: parent
                        text: "Back"
                        font.pixelSize: 13
                        color: theme.textSecondary
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            onboardingPage.statusMessage = ""
                            onboardingPage.currentStep = Math.max(0, onboardingPage.currentStep - 1)
                        }
                        Accessible.name: "Go back to the previous step"
                        Accessible.role: Accessible.Button
                    }
                }

                Item { Layout.fillWidth: true }

                // Skip (hidden on last step)
                Rectangle {
                    visible: onboardingPage.currentStep < 3
                    width: skipLabel.implicitWidth + 32
                    height: 40
                    radius: theme.radiusMd
                    color: "transparent"

                    Text {
                        id: skipLabel
                        anchors.centerIn: parent
                        text: "Skip for now"
                        font.pixelSize: 12
                        color: theme.textMuted
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: onboardingPage.finishWizard()
                        Accessible.name: "Skip onboarding and finish"
                        Accessible.role: Accessible.Button
                    }
                }

                // Next / Finish
                Rectangle {
                    width: nextLabel.implicitWidth + 40
                    height: 40
                    radius: theme.radiusMd
                    color: onboardingPage.saving ? theme.surfaceAlt : theme.accent

                    Text {
                        id: nextLabel
                        anchors.centerIn: parent
                        text: onboardingPage.currentStep === 3 ? "Finish setup" : "Continue"
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        color: "#ffffff"
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        enabled: !onboardingPage.saving
                        onClicked: {
                            if (onboardingPage.currentStep === 1) {
                                // Persist the chosen content folder before moving on.
                                onboardingPage.saveContentFolder()
                            }
                            if (onboardingPage.currentStep === 3) {
                                onboardingPage.finishWizard()
                            } else {
                                onboardingPage.statusMessage = ""
                                onboardingPage.currentStep = Math.min(3, onboardingPage.currentStep + 1)
                            }
                        }
                        Accessible.name: onboardingPage.currentStep === 3 ? "Finish onboarding setup" : "Continue to the next step"
                        Accessible.role: Accessible.Button
                    }
                }
            }
        }
    }

    // ── Actions ───────────────────────────────────────────────────

    function saveContentFolder() {
        if (typeof controller === "undefined" || !controller.saveOnboarding)
            return
        if (onboardingPage.contentFolder.length === 0)
            return
        onboardingPage.saving = true
        try {
            var payload = JSON.stringify({ local: { path: onboardingPage.contentFolder } })
            var raw = controller.saveOnboarding(payload)
            var result = JSON.parse(raw)
            if (!result.ok) {
                onboardingPage.statusMessage = result.error || "Could not save folder"
            }
        } catch(e) {
            console.warn('OnboardingPage: saveContentFolder failed', e)
            onboardingPage.statusMessage = "Could not save folder"
        } finally {
            onboardingPage.saving = false
        }
    }

    function finishWizard() {
        onboardingPage.saving = true
        if (typeof controller !== "undefined" && controller.markOnboardingComplete) {
            try {
                var raw = controller.markOnboardingComplete()
                var result = JSON.parse(raw)
                if (!result.ok) {
                    onboardingPage.statusMessage = result.error || "Could not complete onboarding"
                    onboardingPage.saving = false
                    return
                }
            } catch(e) {
                console.warn('OnboardingPage: markOnboardingComplete failed', e)
                onboardingPage.statusMessage = "Could not complete onboarding"
                onboardingPage.saving = false
                return
            }
        }
        onboardingPage.saving = false
        // Route to the dashboard once onboarding is complete.
        if (typeof root !== "undefined" && root.navigateTo)
            root.navigateTo("dashboard")
        else if (typeof showToast !== "undefined")
            showToast("Setup complete!", false)
    }
}

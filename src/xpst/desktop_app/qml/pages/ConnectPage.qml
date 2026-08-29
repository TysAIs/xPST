import xpst.desktop_app.qml 1.0
import QtQuick 2.15
import xpst.desktop_app.qml 1.0
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: connectPage
    background: Rectangle { color: theme.canvas }

    property var healthData: {
        try {
            if (typeof controller !== "undefined" && controller.platformHealth)
                return JSON.parse(controller.platformHealth)
        } catch(e) { console.error('ConnectPage: failed to parse platformHealth', e); if (typeof showToast !== "undefined") showToast("Could not load platform status", true) }
        return ({})
    }
    property var providerCatalog: ({ sources: [], destinations: [] })
    property var destinationProviders: fallbackDestinations()
    property var sourceProviders: fallbackSources()
    property var readinessData: ({ ready: true, summary: "", blocking: [], warnings: [] })
    property var connectingPlatforms: ({})
    property var connectingStates: ({})   // platform -> "connecting" | "waiting_for_browser"
    property string onboardingLocalPath: ""
    property string onboardingTikTokUsername: ""
    property string igUsername: ""
    property string igPassword: ""
    property bool onboardingYouTube: true
    property bool onboardingInstagram: true
    property bool onboardingX: true

    // Refresh health when data changes
    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onDataChanged() {
            try {
                connectPage.healthData = JSON.parse(controller.platformHealth)
            } catch(e) { console.error('ConnectPage: onDataChanged failed to update healthData', e) }
            connectPage.loadProviders()
            connectPage.loadReadiness()
        }
    }

    Component.onCompleted: {
        loadProviders()
        loadReadiness()
    }

    function fallbackDestinations() {
        return [
            { display_name: "YouTube Shorts", name: "youtube", auth_mode: "oauth", provider_mode: "official", capabilities: ["upload", "delete", "health"] },
            { display_name: "Instagram Reels", name: "instagram", auth_mode: "session", provider_mode: "community", capabilities: ["upload", "delete", "carousel", "health"] },
            { display_name: "X", name: "x", auth_mode: "cookies", provider_mode: "community", capabilities: ["upload", "delete", "carousel", "health"] }
        ]
    }

    function fallbackSources() {
        return [
            { display_name: "TikTok", name: "tiktok", auth_mode: "cookies", provider_mode: "community", capabilities: ["list", "download", "carousel", "health"] },
            { display_name: "Local Files", name: "local", auth_mode: "local", provider_mode: "official", capabilities: ["list", "download", "carousel", "local_only"] }
        ]
    }

    // Human-friendly sign-in label per platform (replaces raw auth_mode in UI).
    function signInLabel(providerName, authMode) {
        var p = (providerName || "").toLowerCase()
        var m = (authMode || "").toLowerCase()
        if (p === "youtube" || m === "oauth") return "Sign in with Google"
        if (p === "instagram") return "Sign in with Instagram"
        if (p === "x") return "Sign in with X"
        if (p === "tiktok") return "Sign in with TikTok"
        if (p === "local" || m === "local") return "Local folder"
        if (m === "graph_api") return "Sign in with Meta (official)"
        return "Sign in"
    }

    // True when a platform is community-supported (vs official API).
    function isCommunityPlatform(providerName, providerMode) {
        var mode = (providerMode || "").toLowerCase()
        if (mode === "community") return true
        if (mode === "official") return false
        // Fall back to known community platforms.
        var p = (providerName || "").toLowerCase()
        return p === "instagram" || p === "x" || p === "tiktok"
    }

    // Human-friendly feature summary shown as icons/labels (replaces raw
    // capabilities array in the UI).
    function featureSummary(capabilities) {
        if (!capabilities || capabilities.length === 0) return "Health check"
        var labels = []
        for (var i = 0; i < capabilities.length; i++) {
            var cap = String(capabilities[i])
            if (cap === "upload") labels.push("Upload")
            else if (cap === "delete") labels.push("Delete")
            else if (cap === "carousel") labels.push("Carousel")
            else if (cap === "health") labels.push("Health check")
            else if (cap === "list") labels.push("Browse")
            else if (cap === "download") labels.push("Download")
            else if (cap === "local_only") continue
            else labels.push(cap.replace("_", " "))
            if (labels.length >= 4) break
        }
        return labels.length > 0 ? labels.join(" · ") : "Health check"
    }

    // Human-friendly credential status per platform (replaces raw health
    // plumbing like circuit_breaker_open / last_success timestamps).
    function credentialStatus(providerName, status) {
        if (!status.connected) {
            if (status.circuitBreakerOpen) return "Connection issue — click to retry"
            if (!status.enabled) return "Disabled"
            return "Not connected"
        }
        if (status.lastSuccess) {
            try {
                var d = new Date(status.lastSuccess)
                var now = new Date()
                var diff = (now - d) / 1000
                if (diff < 60) return "Connected just now"
                if (diff < 3600) return "Connected " + Math.floor(diff / 60) + "m ago"
                if (diff < 86400) return "Connected " + Math.floor(diff / 3600) + "h ago"
                return "Connected " + Math.floor(diff / 86400) + "d ago"
            } catch(e) { return "Connected" }
        }
        return "Connected"
    }

    function loadProviders() {
        if (typeof controller === "undefined" || !controller.getProviders)
            return
        try {
            var raw = controller.getProviders()
            var parsed = JSON.parse(raw)
            if (parsed.ok) {
                connectPage.providerCatalog = parsed
                connectPage.destinationProviders = parsed.destinations && parsed.destinations.length > 0
                                              ? parsed.destinations : fallbackDestinations()
                connectPage.sourceProviders = parsed.sources && parsed.sources.length > 0
                                          ? parsed.sources : fallbackSources()
            }
        } catch(e) { console.error('ConnectPage: loadProviders failed', e); if (typeof showToast !== "undefined") showToast("Could not load platform providers", true) }
    }

    function loadReadiness() {
        if (typeof controller === "undefined" || !controller.getReadiness)
            return
        try {
            var raw = controller.getReadiness()
            var parsed = JSON.parse(raw)
            if (parsed.ok && parsed.readiness)
                connectPage.readinessData = parsed.readiness
        } catch(e) { console.error('ConnectPage: loadReadiness failed', e); if (typeof showToast !== "undefined") showToast("Could not check setup readiness", true) }
    }

    function setupItems() {
        var items = []
        var blocking = connectPage.readinessData.blocking || []
        var warnings = connectPage.readinessData.warnings || []
        for (var i = 0; i < blocking.length; i++)
            items.push(blocking[i])
        for (var j = 0; j < warnings.length; j++)
            items.push(warnings[j])
        return items
    }

    function providerIcon(providerName) {
        var p = (providerName || "").toLowerCase()
        if (p === "youtube") return theme.iconYouTube
        if (p === "instagram") return theme.iconInstagram
        if (p === "x") return theme.iconX
        if (p === "tiktok") return theme.iconTikTok
        if (p === "local") return "..."
        return "+"
    }

    function providerColor(providerName) {
        var p = (providerName || "").toLowerCase()
        if (p === "youtube") return theme.youtube
        if (p === "instagram") return theme.instagram
        if (p === "x") return theme.xtwitter
        if (p === "tiktok") return theme.tiktok
        return theme.accent
    }

    function formatCapabilities(capabilities) {
        if (!capabilities || capabilities.length === 0) return "health"
        var labels = []
        for (var i = 0; i < capabilities.length && labels.length < 4; i++) {
            var cap = String(capabilities[i]).replace("_", " ")
            if (cap === "official api" || cap === "cookie auth" || cap === "oauth")
                continue
            labels.push(cap)
        }
        return labels.join(" / ")
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
        connectPage.connectingStates[platformName.toLowerCase()] = "connecting"
        connectPage.connectingPlatformsChanged()
        connectPage.connectingStatesChanged()
        controller.connectPlatformAsync(platformName.toLowerCase())
    }

    function saveFirstRunChoices() {
        if (typeof controller === "undefined" || !controller.saveOnboarding)
            return
        try {
            var source = onboardingLocalPath.length > 0
                       ? { type: "local", path: onboardingLocalPath }
                       : { type: "tiktok", username: onboardingTikTokUsername }
            var raw = controller.saveOnboarding(JSON.stringify({
                source: source,
                destinations: {
                    youtube: onboardingYouTube,
                    instagram: onboardingInstagram,
                    x: onboardingX
                }
            }))
            var result = JSON.parse(raw)
            if (result.ok) {
                if (result.readiness)
                    connectPage.readinessData = result.readiness
                if (typeof showToast !== "undefined")
                    showToast("Setup saved", false)
                controller.refreshData()
            } else if (typeof showToast !== "undefined") {
                showToast(result.error || "Could not save setup", true)
            }
        } catch(e) {
            console.warn("ConnectPage: failed to save onboarding setup", e)
            if (typeof showToast !== "undefined")
                showToast("Could not save setup", true)
        }
    }

    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onConnectResult(jsonStr) {
            try {
                var result = JSON.parse(jsonStr)
                var key = result.platform || ""
                connectPage.connectingPlatforms[key] = false
                connectPage.connectingStates[key] = ""
                connectPage.connectingPlatformsChanged()
                connectPage.connectingStatesChanged()
                if (result.ok === false && typeof showToast !== "undefined") {
                    showToast(result.error || ("Could not connect " + key), true)
                } else if (result.ok === true && typeof showToast !== "undefined") {
                    showToast(result.message || ("Connected to " + key), false)
                }
                controller.refreshData()
            } catch(e) {
                console.warn("ConnectPage: failed to parse connectResult, resetting connection state", e)
                connectPage.connectingPlatforms = ({})
                connectPage.connectingStates = ({})
            }
        }
    }

    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onConnectStateChanged(jsonStr) {
            try {
                var s = JSON.parse(jsonStr)
                var key = s.platform || ""
                if (s.state === "waiting_for_browser") {
                    connectPage.connectingStates[key] = "waiting_for_browser"
                    connectPage.connectingStatesChanged()
                } else if (s.state === "success" || s.state === "error") {
                    connectPage.connectingPlatforms[key] = false
                    connectPage.connectingStates[key] = ""
                    connectPage.connectingPlatformsChanged()
                    connectPage.connectingStatesChanged()
                }
            } catch(e) {
                console.warn("ConnectPage: failed to parse connectStateChanged", e)
            }
        }
    }

    // X sign-in helper dialog (C1.8): replaces a raw JSON paste box with
    // simple, human-friendly instructions. The paste field is kept for
    // advanced users but framed as optional.
    Dialog {
        id: xCookieDialog
        anchors.centerIn: parent
        width: Math.min(520, parent.width - 60)
        height: Math.min(460, parent.height - 60)
        modal: true
        title: "Sign in with X"
        closePolicy: Popup.CloseOnEscape

        contentItem: ColumnLayout {
            spacing: theme.spacingMd

            Text {
                text: "To connect X (Twitter), sign in to X in your browser, then copy your session cookies."
                font.pixelSize: 12
                color: theme.textSecondary
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Text {
                text: "1. Open <a href='https://x.com'>x.com</a> and sign in\n2. Use a cookie export extension (e.g. EditThisCookie)\n3. Copy the cookies as JSON\n4. Paste them below and click Save"
                font.pixelSize: 12
                color: theme.textPrimary
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                textFormat: Text.RichText
                onLinkActivated: Qt.openUrlExternally(link)
            }

            Text {
                text: "Cookies are stored encrypted in ~/.xpst/credentials/ and never leave this device."
                font.pixelSize: 11
                color: theme.textMuted
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: theme.radiusMd
                color: theme.surfaceAlt
                border.color: theme.textMuted
                border.width: 1

                Flickable {
                    anchors.fill: parent
                    anchors.margins: theme.spacingSm
                    clip: true
                    contentHeight: cookieInput.implicitHeight

                    TextEdit {
                        id: cookieInput
                        width: parent.width
                        color: theme.textPrimary
                        font.pixelSize: 11
                        font.family: theme.monoFontFamily
                        wrapMode: TextEdit.Wrap
                        property string placeholderText: 'Paste copied cookies here (optional, advanced)'

                        Text {
                            anchors.fill: parent
                            text: cookieInput.placeholderText
                            font: cookieInput.font
                            color: theme.textMuted
                            visible: cookieInput.text.length === 0
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd

                Item { Layout.fillWidth: true }

                Rectangle {
                    width: xCookieCancelLabel.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: xCancelMouse.containsMouse ? theme.accentHover : theme.surfaceAlt
                    Behavior on color { ColorAnimation { duration: 120 } }
                    Text {
                        id: xCookieCancelLabel
                        anchors.centerIn: parent
                        text: "Cancel"
                        font.pixelSize: 12
                        color: theme.textSecondary
                    }
                    MouseArea {
                        id: xCancelMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            cookieInput.text = ""
                            xCookieDialog.close()
                        }
                    }
                }

                Rectangle {
                    width: xCookieSaveLabel.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: xSaveMouse.containsPress ? theme.accentMuted
                         : xSaveMouse.containsMouse ? theme.accentHover
                         : theme.accent
                    Behavior on color { ColorAnimation { duration: 120 } }
                    Text {
                        id: xCookieSaveLabel
                        anchors.centerIn: parent
                        text: "Save Cookies"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        color: "#ffffff"
                    }
                    MouseArea {
                        id: xSaveMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (cookieInput.text.length > 0 && typeof controller !== "undefined") {
                                var settings = { x_cookies: cookieInput.text }
                                controller.saveSettings(JSON.stringify(settings))
                                if (typeof showToast !== "undefined") showToast("X cookies saved", false)
                            }
                            cookieInput.text = ""
                            xCookieDialog.close()
                        }
                    }
                }
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
            anchors.margins: theme.pageMargin
            spacing: theme.spacingXl

            // Header
            ColumnLayout {
                spacing: theme.spacingXs
                Text {
                    text: "Accounts"
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Layout.fillWidth: true
                    Accessible.name: "Accounts page title"
                    Accessible.role: Accessible.Heading
                }
                Text {
                    text: "Manage your social media connections"
                    font.pixelSize: 13
                    color: theme.textSecondary
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: firstRunSetupContent.implicitHeight + theme.spacingXl
                radius: theme.radiusLg
                color: theme.surfaceCard

                ColumnLayout {
                    id: firstRunSetupContent
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: theme.pageMargin
                    spacing: theme.spacingMd

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingXs

                            Text {
                                text: "First-run setup"
                                font.pixelSize: 16
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                                Accessible.name: "First-run setup"
                                Accessible.role: Accessible.Heading
                            }

                            Text {
                                text: "Pick a source and destination platforms"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                        }

                        Rectangle {
                            width: saveSetupLabel.implicitWidth + 28
                            height: 34
                            radius: theme.radiusMd
                            color: saveSetupMouse.containsPress ? theme.accentMuted
                                 : saveSetupMouse.containsMouse ? theme.accentHover
                                 : theme.accent
                            Behavior on color { ColorAnimation { duration: 120 } }

                            Text {
                                id: saveSetupLabel
                                anchors.centerIn: parent
                                text: "Save setup"
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                color: "#ffffff"
                            }

                            MouseArea {
                                id: saveSetupMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: connectPage.saveFirstRunChoices()
                                Accessible.name: "Save setup"
                                Accessible.role: Accessible.Button
                            }
                        }
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        columnSpacing: theme.spacingXl
                        rowSpacing: theme.spacingMd

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingXs
                            Text {
                                text: "Local folder"
                                font.pixelSize: 12
                                color: theme.textMuted
                            }
                            TextField {
                                Layout.fillWidth: true
                                text: connectPage.onboardingLocalPath
                                placeholderText: "C:/Videos"
                                selectByMouse: true
                                color: theme.textPrimary
                                placeholderTextColor: theme.textMuted
                                onTextChanged: connectPage.onboardingLocalPath = text
                                background: Rectangle {
                                    radius: theme.radiusMd
                                    color: theme.surfaceAlt
                                    border.color: theme.textMuted
                                    border.width: 1
                                }
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingXs
                            Text {
                                text: "TikTok username"
                                font.pixelSize: 12
                                color: theme.textMuted
                            }
                            TextField {
                                Layout.fillWidth: true
                                text: connectPage.onboardingTikTokUsername
                                placeholderText: "username"
                                selectByMouse: true
                                color: theme.textPrimary
                                placeholderTextColor: theme.textMuted
                                onTextChanged: connectPage.onboardingTikTokUsername = text
                                background: Rectangle {
                                    radius: theme.radiusMd
                                    color: theme.surfaceAlt
                                    border.color: theme.textMuted
                                    border.width: 1
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingLg

                        CheckBox {
                            text: "YouTube"
                            checked: connectPage.onboardingYouTube
                            onToggled: connectPage.onboardingYouTube = checked
                        }
                        CheckBox {
                            text: "Instagram"
                            checked: connectPage.onboardingInstagram
                            onToggled: connectPage.onboardingInstagram = checked
                        }
                        CheckBox {
                            text: "X"
                            checked: connectPage.onboardingX
                            onToggled: connectPage.onboardingX = checked
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: setupChecklistContent.implicitHeight + theme.spacingXl
                radius: theme.radiusLg
                color: connectPage.readinessData.ready ? Qt.rgba(0.20, 0.72, 0.48, 0.10) : theme.surfaceCard
                border.color: connectPage.readinessData.ready ? theme.success : theme.warning
                border.width: 1

                ColumnLayout {
                    id: setupChecklistContent
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: theme.pageMargin
                    spacing: theme.spacingMd

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingXs

                            Text {
                                text: connectPage.readinessData.ready ? "Ready to post" : "Setup checklist"
                                font.pixelSize: 16
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                                Accessible.name: text
                                Accessible.role: Accessible.Heading
                            }

                            Text {
                                text: connectPage.readinessData.summary || ""
                                font.pixelSize: 12
                                color: theme.textSecondary
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }
                        }

                        Rectangle {
                            width: refreshChecklistLabel.implicitWidth + 28
                            height: 34
                            radius: theme.radiusMd
                            color: refreshChecklistMouse.containsPress ? theme.accentMuted
                                 : refreshChecklistMouse.containsMouse ? theme.accentHover
                                 : theme.surfaceAlt
                            Behavior on color { ColorAnimation { duration: 120 } }

                            Text {
                                id: refreshChecklistLabel
                                anchors.centerIn: parent
                                text: "Refresh"
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                color: theme.textSecondary
                            }

                            MouseArea {
                                id: refreshChecklistMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: connectPage.loadReadiness()
                                Accessible.name: "Refresh setup checklist"
                                Accessible.role: Accessible.Button
                            }
                        }
                    }

                    Repeater {
                        model: connectPage.setupItems()

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingSm

                            Rectangle {
                                width: 8
                                height: 8
                                radius: 4
                                color: modelData.severity === "error" ? theme.error : theme.warning
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2

                                Text {
                                    text: modelData.label || ""
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    color: theme.textPrimary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }

                                Text {
                                    text: modelData.action && modelData.action.length > 0 ? modelData.action : (modelData.message || "")
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }
            }

            // Platform cards
            GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: theme.spacingXl
                rowSpacing: theme.spacingXl

                Repeater {
                    model: connectPage.destinationProviders

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 224
                        radius: theme.radiusXl
                        color: theme.surfaceCard

                        property string providerKey: modelData.name || ""
                        property string providerName: modelData.display_name || modelData.name || ""
                        property var platformStatus: connectPage.getPlatformStatus(providerKey)
                        property bool isConnecting: connectPage.connectingPlatforms[providerKey] === true

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: theme.pageMargin
                            spacing: theme.spacingMd

                            RowLayout {
                                spacing: theme.spacingMd

                                Rectangle {
                                    width: 48; height: 48; radius: theme.radiusLg
                                    color: Qt.rgba(connectPage.providerColor(providerKey).r, connectPage.providerColor(providerKey).g, connectPage.providerColor(providerKey).b, 0.15)
                                    Text {
                                        anchors.centerIn: parent
                                        text: connectPage.providerIcon(providerKey)
                                        font.family: theme.iconFontFamily
                                        font.pixelSize: 14
                                        color: connectPage.providerColor(providerKey)
                                    }
                                }

                                ColumnLayout {
                                    spacing: theme.spacingXs
                                    Text {
                                        text: providerName
                                        font.pixelSize: 16
                                        font.weight: Font.DemiBold
                                        color: theme.textPrimary
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
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

                            RowLayout {
                                spacing: theme.spacingSm
                                Text {
                                    text: "Sign in:"
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                                Text {
                                    text: connectPage.signInLabel(providerKey, modelData.auth_mode)
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }

                                // Provider Mode badge (C1.4): green "Official"
                                // or yellow "Community".
                                Rectangle {
                                    width: modeBadgeText.implicitWidth + 16
                                    height: 18
                                    radius: 9
                                    color: connectPage.isCommunityPlatform(providerKey, modelData.provider_mode)
                                           ? Qt.rgba(theme.warning.r, theme.warning.g, theme.warning.b, 0.18)
                                           : Qt.rgba(theme.success.r, theme.success.g, theme.success.b, 0.18)
                                    Text {
                                        id: modeBadgeText
                                        anchors.centerIn: parent
                                        text: connectPage.isCommunityPlatform(providerKey, modelData.provider_mode) ? "Community" : "Official"
                                        font.pixelSize: 10
                                        font.weight: Font.DemiBold
                                        color: connectPage.isCommunityPlatform(providerKey, modelData.provider_mode) ? theme.warning : theme.success
                                    }
                                }
                            }

                            // Credential status (C1.5): human-friendly line
                            // replacing raw circuit_breaker_open / timestamps.
                            RowLayout {
                                spacing: theme.spacingXs
                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    color: platformStatus.connected ? theme.success : (platformStatus.circuitBreakerOpen ? theme.warning : theme.textMuted)
                                }
                                Text {
                                    text: connectPage.credentialStatus(providerKey, platformStatus)
                                    font.pixelSize: 12
                                    color: platformStatus.circuitBreakerOpen ? theme.warning : theme.textSecondary
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                }
                            }

                            // Health status
                            RowLayout {
                                spacing: theme.spacingSm
                                Text {
                                    text: "Status:"
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
                                    text: platformStatus.failures > 0 ? "(" + platformStatus.failures + " recent issues)" : ""
                                    font.pixelSize: 11
                                    color: theme.error
                                    visible: platformStatus.failures > 0
                                }

                                Item { Layout.fillWidth: true }
                            }

                            // Feature summary (C1.2): replaces raw capabilities
                            // array with a human-friendly feature list.
                            RowLayout {
                                spacing: theme.spacingSm
                                Text {
                                    text: "Supports:"
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                                Text {
                                    text: connectPage.featureSummary(modelData.capabilities)
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                            }

                            Item { Layout.fillHeight: true }

                            // Connect/Disconnect button
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusMd
                                color: isConnecting ? theme.surfaceAlt
                                     : platformStatus.connected
                                       ? (connectBtnMouse.containsMouse ? theme.accentHover : theme.surfaceAlt)
                                       : (connectBtnMouse.containsPress ? theme.accentMuted
                                          : connectBtnMouse.containsMouse ? theme.accentHover : theme.accent)
                                Behavior on color { ColorAnimation { duration: 120 } }

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
                                        text: {
                                            if (!isConnecting) return platformStatus.connected ? "Disconnect" : "Connect"
                                            var st = connectPage.connectingStates[providerKey]
                                            return st === "waiting_for_browser" ? "Waiting for browser…" : "Connecting..."
                                        }
                                        font.pixelSize: 13
                                        font.weight: Font.DemiBold
                                        color: platformStatus.connected ? theme.textSecondary : "#ffffff"
                                    }
                                }

                                MouseArea {
                                    id: connectBtnMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    enabled: !isConnecting
                                    onClicked: connectPage.connectPlatform(providerKey)
                                    Accessible.name: (isConnecting ? "Connecting" : (platformStatus.connected ? "Disconnect from " : "Connect to ")) + providerName
                                    Accessible.role: Accessible.Button
                                }
                            }

                            // Sign-in helper button (X platform only)
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: providerKey === "x" ? 32 : 0
                                radius: theme.radiusMd
                                color: xSignInMouse.containsMouse ? theme.accentHover : theme.surfaceAlt
                                Behavior on color { ColorAnimation { duration: 120 } }
                                visible: providerKey === "x"
                                clip: true

                                Text {
                                    anchors.centerIn: parent
                                    text: "Sign in with X"
                                    font.pixelSize: 11
                                    color: theme.textSecondary
                                }

                                MouseArea {
                                    id: xSignInMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: xCookieDialog.open()
                                    Accessible.name: "Sign in with X"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }
                    }
                }
            }

            // ── Platform-Specific Setup Guides ──────────────────────
            ColumnLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd

                Text {
                    text: "Platform Setup Guides"
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: text
                    Accessible.role: Accessible.Heading
                }

                Text {
                    text: "Step-by-step instructions for connecting each platform."
                    font.pixelSize: 13
                    color: theme.textSecondary
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                }

                // YouTube Setup Guide
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: ytGuideCol.implicitHeight + theme.spacingXl
                    radius: theme.radiusLg
                    color: theme.surfaceCard
                    border.color: theme.textMuted
                    border.width: 1

                    ColumnLayout {
                        id: ytGuideCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.margins: theme.pageMargin
                        spacing: theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingMd

                            Text {
                                text: theme.iconYouTube || Icons.play
                                font.family: theme.iconFontFamily
                                font.pixelSize: 18
                                color: "#FF0000"
                            }
                            Text {
                                text: "YouTube Shorts (OAuth 2.0)"
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                            Item { Layout.fillWidth: true }
                            Rectangle {
                                width: ytConnectBtn.implicitWidth + 24
                                height: 34
                                radius: theme.radiusMd
                                color: connectPage.connectingPlatforms.youtube ? theme.surfaceAlt
                                     : ytConnectMouse.containsPress ? theme.accentMuted
                                     : ytConnectMouse.containsMouse ? theme.accentHover
                                     : theme.accent
                                Behavior on color { ColorAnimation { duration: 120 } }
                                visible: typeof controller !== "undefined"

                                Text {
                                    id: ytConnectBtn
                                    anchors.centerIn: parent
                                    text: {
                                        if (!connectPage.connectingPlatforms.youtube) return "Connect YouTube"
                                        return connectPage.connectingStates.youtube === "waiting_for_browser" ? "Waiting for browser…" : "Connecting..."
                                    }
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    color: connectPage.connectingPlatforms.youtube ? theme.textSecondary : "#ffffff"
                                }

                                MouseArea {
                                    id: ytConnectMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: connectPage.connectPlatform("youtube")
                                    Accessible.name: "Connect YouTube"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }

                        Text {
                            text: "1. Go to <a href='https://console.cloud.google.com'>Google Cloud Console</a> and create/select a project"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                            onLinkActivated: Qt.openUrlExternally(link)
                        }
                        Text {
                            text: "2. Enable the <b>YouTube Data API v3</b> from the API Library"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "3. Go to Credentials → Create Credentials → <b>OAuth 2.0 Client ID</b> (Application type: Desktop app)"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "4. Download the <code>client_secret_*.json</code> file"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "5. Place it at <code>~/.xpst/credentials/youtube_client_secret.json</code>"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "6. Click <b>Connect YouTube</b> above — a browser window opens for OAuth consent"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "Note: 2FA/2SV on your Google account is fine — the OAuth flow handles it. Default quota: 10,000 units/day."
                            font.pixelSize: 11
                            color: theme.textMuted
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            font.italic: true
                        }
                    }
                }

                // Instagram Setup Guide
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: igGuideCol.implicitHeight + theme.spacingXl
                    radius: theme.radiusLg
                    color: theme.surfaceCard
                    border.color: theme.textMuted
                    border.width: 1

                    ColumnLayout {
                        id: igGuideCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.margins: theme.pageMargin
                        spacing: theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingMd

                            Text {
                                text: theme.iconInstagram || Icons.camera
                                font.family: theme.iconFontFamily
                                font.pixelSize: 18
                                color: "#E1306C"
                            }
                            Text {
                                text: "Instagram Reels (Session-based)"
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                            Item { Layout.fillWidth: true }
                        }

                        Text {
                            text: "1. Log into <a href='https://instagram.com'>instagram.com</a> in your browser first"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                            onLinkActivated: Qt.openUrlExternally(link)
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingSm

                            Text {
                                text: "2. Username:"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            TextField {
                                id: igUsernameField
                                Layout.preferredWidth: 160
                                placeholderText: "username"
                                font.pixelSize: 12
                                color: theme.textPrimary
                                background: Rectangle {
                                    color: theme.surfaceAlt
                                    border.color: theme.textMuted
                                    border.width: 1
                                    radius: theme.radiusSm
                                }
                                onTextChanged: {
                                    connectPage.igUsername = text
                                }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingSm

                            Text {
                                text: "3. Password:"
                                font.pixelSize: 12
                                color: theme.textSecondary
                            }
                            TextField {
                                id: igPasswordField
                                Layout.preferredWidth: 160
                                placeholderText: "password"
                                echoMode: TextInput.Password
                                font.pixelSize: 12
                                color: theme.textPrimary
                                background: Rectangle {
                                    color: theme.surfaceAlt
                                    border.color: theme.textMuted
                                    border.width: 1
                                    radius: theme.radiusSm
                                }
                                onTextChanged: {
                                    connectPage.igPassword = text
                                }
                            }
                        }

                        Text {
                            text: "4. Click <b>Connect Instagram</b> — credentials are stored encrypted in ~/.xpst/credentials/"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingSm

                            Rectangle {
                                width: igConnectBtn.implicitWidth + 24
                                height: 34
                                radius: theme.radiusMd
                                color: connectPage.connectingPlatforms.instagram ? theme.surfaceAlt
                                     : igConnectMouse.containsPress ? theme.accentMuted
                                     : igConnectMouse.containsMouse ? theme.accentHover
                                     : theme.accent
                                Behavior on color { ColorAnimation { duration: 120 } }
                                visible: typeof controller !== "undefined"

                                Text {
                                    id: igConnectBtn
                                    anchors.centerIn: parent
                                    text: {
                                        if (!connectPage.connectingPlatforms.instagram) return "Connect Instagram"
                                        return connectPage.connectingStates.instagram === "waiting_for_browser" ? "Waiting for browser…" : "Connecting..."
                                    }
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    color: connectPage.connectingPlatforms.instagram ? theme.textSecondary : "#ffffff"
                                }

                                MouseArea {
                                    id: igConnectMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: connectPage.connectPlatform("instagram")
                                    Accessible.name: "Connect Instagram"
                                    Accessible.role: Accessible.Button
                                }
                            }
                            Item { Layout.fillWidth: true }
                        }

                        Text {
                            text: "Note: Use a dedicated account. Carousel uploads support up to 10 images/videos."
                            font.pixelSize: 11
                            color: theme.textMuted
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            font.italic: true
                        }
                    }
                }

                // X/Twitter Setup Guide
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: xGuideCol.implicitHeight + theme.spacingXl
                    radius: theme.radiusLg
                    color: theme.surfaceCard
                    border.color: theme.textMuted
                    border.width: 1

                    ColumnLayout {
                        id: xGuideCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.margins: theme.pageMargin
                        spacing: theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingMd

                            Text {
                                text: theme.iconX || Icons.share
                                font.family: theme.iconFontFamily
                                font.pixelSize: 18
                                color: "#000000"
                            }
                            Text {
                                text: "X / Twitter (Cookie-based)"
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                            Item { Layout.fillWidth: true }
                            Rectangle {
                                width: xConnectBtn.implicitWidth + 24
                                height: 34
                                radius: theme.radiusMd
                                color: xPasteMouse.containsMouse ? theme.accentHover : theme.surfaceAlt
                                Behavior on color { ColorAnimation { duration: 120 } }

                                Text {
                                    id: xConnectBtn
                                    anchors.centerIn: parent
                                    text: "Paste Cookies"
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    color: theme.textSecondary
                                }

                                MouseArea {
                                    id: xPasteMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: xCookieDialog.open()
                                    Accessible.name: "Paste X cookies"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }

                        Text {
                            text: "1. Log into <a href='https://x.com'>x.com</a> in your browser"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                            onLinkActivated: Qt.openUrlExternally(link)
                        }
                        Text {
                            text: "2. Export cookies using a browser extension (e.g., EditThisCookie) or DevTools"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                        }
                        Text {
                            text: "3. Place cookies JSON at <code>~/.xpst/credentials/x_cookies.json</code>"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "4. Or run <code>xpst auth x</code> in terminal for guided setup"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "Note: X API tier limits apply. Free tier allows limited posts/day."
                            font.pixelSize: 11
                            color: theme.textMuted
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            font.italic: true
                        }
                    }
                }

                // TikTok Setup Guide
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: ttGuideCol.implicitHeight + theme.spacingXl
                    radius: theme.radiusLg
                    color: theme.surfaceCard
                    border.color: theme.textMuted
                    border.width: 1

                    ColumnLayout {
                        id: ttGuideCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.margins: theme.pageMargin
                        spacing: theme.spacingSm

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.spacingMd

                            Text {
                                text: theme.iconTikTok || Icons.play
                                font.family: theme.iconFontFamily
                                font.pixelSize: 18
                                color: "#010101"
                            }
                            Text {
                                text: "TikTok (Source Only — Cookie-based)"
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                                color: theme.textPrimary
                            }
                            Item { Layout.fillWidth: true }
                            Rectangle {
                                width: ttConnectBtn.implicitWidth + 24
                                height: 34
                                radius: theme.radiusMd
                                color: ttPasteMouse.containsMouse ? theme.accentHover : theme.surfaceAlt
                                Behavior on color { ColorAnimation { duration: 120 } }

                                Text {
                                    id: ttConnectBtn
                                    anchors.centerIn: parent
                                    text: "Paste Cookies"
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    color: theme.textSecondary
                                }

                                MouseArea {
                                    id: ttPasteMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: connectPage.connectPlatform("tiktok")
                                    Accessible.name: "Connect TikTok"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }

                        Text {
                            text: "1. Log into <a href='https://tiktok.com'>tiktok.com</a> in your browser"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                            onLinkActivated: Qt.openUrlExternally(link)
                        }
                        Text {
                            text: "2. Export cookies to <code>~/.xpst/credentials/tiktok_cookies.json</code>"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "3. Or run <code>xpst auth tiktok</code> in terminal for guided setup"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            textFormat: Text.RichText
                        }
                        Text {
                            text: "Note: TikTok is a source only — xPST monitors it for new videos to cross-post."
                            font.pixelSize: 11
                            color: theme.textMuted
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            font.italic: true
                        }
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd

                Text {
                    text: "Content Sources"
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: "Content Sources section title"
                    Accessible.role: Accessible.Heading
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: theme.spacingXl
                    rowSpacing: theme.spacingMd

                    Repeater {
                        model: connectPage.sourceProviders

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 104
                            radius: theme.radiusLg
                            color: theme.surfaceCard

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: theme.spacingLg
                                spacing: theme.spacingMd

                                Rectangle {
                                    width: 40; height: 40; radius: theme.radiusMd
                                    color: Qt.rgba(connectPage.providerColor(modelData.name).r, connectPage.providerColor(modelData.name).g, connectPage.providerColor(modelData.name).b, 0.14)
                                    Text {
                                        anchors.centerIn: parent
                                        text: connectPage.providerIcon(modelData.name)
                                        font.family: theme.iconFontFamily
                                        font.pixelSize: 13
                                        color: connectPage.providerColor(modelData.name)
                                    }
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: theme.spacingXs
                                    Text {
                                        text: modelData.display_name || modelData.name || ""
                                        font.pixelSize: 14
                                        font.weight: Font.DemiBold
                                        color: theme.textPrimary
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                    Text {
                                        text: connectPage.signInLabel(modelData.name, modelData.auth_mode) + " · " + connectPage.featureSummary(modelData.capabilities)
                                        font.pixelSize: 12
                                        color: theme.textSecondary
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
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

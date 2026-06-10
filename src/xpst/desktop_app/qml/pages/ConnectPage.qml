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
    property var providerCatalog: ({ sources: [], destinations: [] })
    property var destinationProviders: fallbackDestinations()
    property var sourceProviders: fallbackSources()
    property var readinessData: ({ ready: true, summary: "", blocking: [], warnings: [] })
    property var connectingPlatforms: ({})
    property string onboardingLocalPath: ""
    property string onboardingTikTokUsername: ""
    property bool onboardingYouTube: true
    property bool onboardingInstagram: true
    property bool onboardingX: true

    // Refresh health when data changes
    Connections {
        target: typeof controller !== "undefined" ? controller : null
        function onDataChanged() {
            try {
                connectPage.healthData = JSON.parse(controller.platformHealth)
            } catch(e) {}
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
            { display_name: "YouTube Shorts", name: "youtube", auth_mode: "oauth", capabilities: ["upload", "delete", "health"] },
            { display_name: "Instagram Reels", name: "instagram", auth_mode: "session", capabilities: ["upload", "delete", "carousel", "health"] },
            { display_name: "X", name: "x", auth_mode: "cookies", capabilities: ["upload", "delete", "carousel", "health"] }
        ]
    }

    function fallbackSources() {
        return [
            { display_name: "TikTok", name: "tiktok", auth_mode: "cookies", capabilities: ["list", "download", "carousel", "health"] },
            { display_name: "Local Files", name: "local", auth_mode: "local", capabilities: ["list", "download", "carousel", "local_only"] }
        ]
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
        } catch(e) {}
    }

    function loadReadiness() {
        if (typeof controller === "undefined" || !controller.getReadiness)
            return
        try {
            var raw = controller.getReadiness()
            var parsed = JSON.parse(raw)
            if (parsed.ok && parsed.readiness)
                connectPage.readinessData = parsed.readiness
        } catch(e) {}
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
        connectPage.connectingPlatformsChanged()
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
                connectPage.connectingPlatformsChanged()
            } catch(e) {
                connectPage.connectingPlatforms = ({})
            }
        }
    }

    // X Cookie Paste Dialog
    Dialog {
        id: xCookieDialog
        anchors.centerIn: parent
        width: Math.min(500, parent.width - 60)
        height: Math.min(400, parent.height - 60)
        modal: true
        title: "Paste X Cookies"
        closePolicy: Popup.CloseOnEscape

        contentItem: ColumnLayout {
            spacing: theme.spacingMd

            Text {
                text: "Paste your X/Twitter cookies (JSON format) below:"
                font.pixelSize: 12
                color: theme.textSecondary
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
                        property string placeholderText: '[{"name":"ct0","value":"..."}, ...]'

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
                    color: theme.surfaceAlt
                    Text {
                        id: xCookieCancelLabel
                        anchors.centerIn: parent
                        text: "Cancel"
                        font.pixelSize: 12
                        color: theme.textSecondary
                    }
                    MouseArea {
                        anchors.fill: parent
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
                    color: theme.accent
                    Text {
                        id: xCookieSaveLabel
                        anchors.centerIn: parent
                        text: "Save Cookies"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        color: "#ffffff"
                    }
                    MouseArea {
                        anchors.fill: parent
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
                    text: "Connect Platforms"
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: "Connect Platforms page title"
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
                            color: theme.accent

                            Text {
                                id: saveSetupLabel
                                anchors.centerIn: parent
                                text: "Save setup"
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                color: "#ffffff"
                            }

                            MouseArea {
                                anchors.fill: parent
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
                            color: theme.surfaceAlt

                            Text {
                                id: refreshChecklistLabel
                                anchors.centerIn: parent
                                text: "Refresh"
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                color: theme.textSecondary
                            }

                            MouseArea {
                                anchors.fill: parent
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
                                        font.pixelSize: 14
                                        font.weight: Font.DemiBold
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
                                    text: "Auth:"
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                                Text {
                                    text: modelData.auth_mode || "unknown"
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
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
                                    text: "Circuit breaker open"
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
                                    text: "Supports:"
                                    font.pixelSize: 12
                                    color: theme.textMuted
                                }
                                Text {
                                    text: connectPage.formatCapabilities(modelData.capabilities)
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
                                        font.weight: Font.DemiBold
                                        color: platformStatus.connected ? theme.textSecondary : "#ffffff"
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    enabled: !isConnecting
                                    onClicked: connectPage.connectPlatform(providerKey)
                                    Accessible.name: (isConnecting ? "Connecting" : (platformStatus.connected ? "Disconnect from " : "Connect to ")) + providerName
                                    Accessible.role: Accessible.Button
                                }
                            }

                            // Paste Cookies button (X platform only)
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: providerKey === "x" ? 32 : 0
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                visible: providerKey === "x"
                                clip: true

                                Text {
                                    anchors.centerIn: parent
                                    text: "Paste cookies"
                                    font.pixelSize: 11
                                    color: theme.textSecondary
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: xCookieDialog.open()
                                    Accessible.name: "Paste X cookies"
                                    Accessible.role: Accessible.Button
                                }
                            }
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
                                        font.pixelSize: 13
                                        font.weight: Font.DemiBold
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
                                        text: (modelData.auth_mode || "unknown") + " / " + connectPage.formatCapabilities(modelData.capabilities)
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

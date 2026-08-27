import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import xpst.desktop_app.qml 1.0
// QtWebEngine is registered at startup when available (main.py); this
// component degrades to an open-in-browser fallback if the import fails
// (WebEngineView is instantiated through a Loader to keep the Chromium
// processes dormant until a video is actually opened).

Dialog {
    id: root
    modal: true
    anchors.centerIn: parent
    width: Math.min(720, parent.width - 60)
    height: Math.min(560, parent.height - 60)
    title: root.titleLabel

    property string videoTitle: ""
    property string platform: ""
    property string embedUrl: ""        // reliable in-app embed (YouTube only)
    property string postUrl: ""         // canonical post URL (any platform)
    property string thumbnail: ""       // image/file URI or remote URL
    property string caption: ""
    property bool hasEmbed: embedUrl.length > 0
    property bool embedFailed: false

    // Reset transient state each time the dialog is opened.
    onOpened: {
        if (embedLoader.active && embedLoader.status === Loader.Ready) {
            if (embedLoader.item && typeof embedLoader.item.setUrl === "function")
                embedLoader.item.setUrl(root.embedUrl)
        }
        embedFailed = false
        embedLoader.active = hasEmbed
    }

    background: Rectangle {
        color: theme.surfaceCard
        radius: theme.radiusXl
    }

    header: Rectangle {
        color: theme.surfaceAlt
        height: 52
        radius: theme.radiusXl
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: theme.spacingLg
            anchors.rightMargin: theme.spacingLg
            spacing: theme.spacingSm

            Text {
                text: root.platform ? root.platform.toUpperCase() : ""
                font.pixelSize: 10
                font.weight: Font.DemiBold
                color: theme.textMuted
                Layout.preferredWidth: 34
            }
            Text {
                id: titleLabel
                text: root.videoTitle || "Video"
                font.pixelSize: 14
                font.weight: Font.DemiBold
                color: theme.textPrimary
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            Text {
                text: "✕"
                font.pixelSize: 16
                color: theme.textSecondary
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.close()
                    Accessible.name: "Close video player"
                    Accessible.role: Accessible.Button
                }
            }
        }
    }

    contentItem: ColumnLayout {
        spacing: theme.spacingMd

        // ── Playback area ────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: "#000000"
            radius: theme.radiusMd
            clip: true

            // Remote embed (YouTube). Loaded lazily — Chromium stays
            // dormant until the first real playback request.
            Loader {
                id: embedLoader
                anchors.fill: parent
                active: root.hasEmbed && !root.embedFailed
                onStatusChanged: {
                    if (status === Loader.Error) {
                        root.embedFailed = true
                        embedLoader.active = false
                    }
                }
                sourceComponent: Component {
                    Item {
                        anchors.fill: parent
                        function setUrl(u) { view.url = u || "" }
                        WebEngineView {
                            id: view
                            anchors.fill: parent
                            url: root.embedUrl
                            backgroundColor: "#000000"
                        }
                    }
                }
            }

            // Fallback: inline thumbnail (X/IG/TikTok or embed failure).
            Item {
                anchors.fill: parent
                visible: !embedLoader.active || root.embedFailed

                Image {
                    id: fallbackThumb
                    anchors.centerIn: parent
                    width: parent.width
                    height: parent.height
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    source: {
                        if (!root.thumbnail) return ""
                        if (root.thumbnail.indexOf("://") >= 0)
                            return controller && controller.getThumbnail ? controller.getThumbnail(root.thumbnail) : root.thumbnail
                        return "file://" + root.thumbnail
                    }
                    visible: status === Image.Ready
                }

                Text {
                    anchors.centerIn: parent
                    text: "▶  " + (root.videoTitle || "Video")
                    font.pixelSize: 16
                    color: "#ffffff"
                    visible: !fallbackThumb.visible
                }
            }
        }

        // ── Caption preview ─────────────────────────────────────────
        Text {
            text: root.caption
            font.pixelSize: 12
            color: theme.textSecondary
            elide: Text.ElideRight
            maximumLineCount: 2
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
            visible: root.caption.length > 0
        }

        // ── Actions ─────────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: theme.spacingMd

            Item { Layout.fillWidth: true }

            Rectangle {
                width: copyLabel.implicitWidth + 32
                height: 36
                radius: theme.radiusMd
                color: theme.surfaceAlt
                Text {
                    id: copyLabel
                    anchors.centerIn: parent
                    text: "Copy link"
                    font.pixelSize: 12
                    color: theme.textSecondary
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (root.postUrl) {
                            Clipboard.text = root.postUrl
                            if (typeof showToast !== "undefined") showToast("Link copied", false)
                        }
                    }
                    Accessible.name: "Copy post link"
                    Accessible.role: Accessible.Button
                }
            }

            Rectangle {
                width: openLabel.implicitWidth + 32
                height: 36
                radius: theme.radiusMd
                color: theme.accent
                Text {
                    id: openLabel
                    anchors.centerIn: parent
                    text: root.hasEmbed && !root.embedFailed ? "Open in browser" : "Open in browser"
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    color: "#ffffff"
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (root.postUrl) controller.openExternal(root.postUrl)
                    }
                    Accessible.name: "Open post in system browser"
                    Accessible.role: Accessible.Button
                }
            }
        }
    }
}

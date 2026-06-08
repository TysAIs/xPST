import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15


Page {
    id: aboutPage
    background: Rectangle { color: theme.canvas }

    function closeDialog() {}

    property var gitLogData: {
        try {
            if (typeof controller !== "undefined") {
                var result = JSON.parse(controller.getGitLog())
                if (result.ok) return result.commits || []
            }
        } catch(e) {}
        return []
    }

    Flickable {
        anchors.fill: parent
        contentHeight: aboutCol.implicitHeight + theme.spacingXxl
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: aboutCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: theme.spacingXl
            spacing: theme.spacingXl

            // Header
            ColumnLayout {
                spacing: theme.spacingSm
                Text {
                    text: "About xPST"
                    font.pixelSize: 20
                    font.bold: true
                    color: theme.textPrimary
                    Accessible.name: "About xPST page title"
                    Accessible.role: Accessible.Heading
                }
            }

            // Logo + Version
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: aboutHeaderCol.implicitHeight + theme.spacingXxl
                radius: theme.radiusLg
                color: theme.surfaceCard

                ColumnLayout {
                    id: aboutHeaderCol
                    anchors.fill: parent
                    anchors.margins: theme.spacingXl
                    spacing: theme.spacingLg

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: theme.spacingMd
                        Text {
                            text: "⚡"
                            font.pixelSize: 32
                        }
                        Text {
                            text: "xPST"
                            font.pixelSize: 28
                            font.bold: true
                            color: theme.textPrimary
                        }
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "Cross-Posting Suite"
                        font.pixelSize: 16
                        color: theme.textSecondary
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: typeof controller !== "undefined" ? "v1.0.0" : "v1.0.0"
                        font.pixelSize: 13
                        color: theme.textMuted
                        Accessible.name: "Version 1.0.0"
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "Enterprise-grade, open-source cross-posting\nfor short-form video"
                        font.pixelSize: 13
                        color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                    }
                }
            }

            // Dependencies
            ColumnLayout {
                spacing: theme.spacingMd
                Text {
                    text: "Dependencies"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                    Accessible.name: "Dependencies section"
                    Accessible.role: Accessible.Heading
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: depsCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: depsCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingSm

                        Repeater {
                            model: [
                                { name: "PySide6", desc: "Qt for Python — UI framework" },
                                { name: "yt-dlp", desc: "Video downloading" },
                                { name: "instagrapi", desc: "Instagram API client" },
                                { name: "twikit", desc: "X/Twitter API client" },
                                { name: "click", desc: "CLI framework" },
                                { name: "rich", desc: "Terminal formatting" },
                                { name: "pyyaml", desc: "YAML config parsing" },
                                { name: "authlib", desc: "OAuth authentication" },
                                { name: "httpx", desc: "Async HTTP client" },
                                { name: "nicegui", desc: "Web dashboard framework" }
                            ]

                            RowLayout {
                                spacing: theme.spacingMd
                                Rectangle {
                                    width: 4; height: 4; radius: 2
                                    color: theme.accent
                                }
                                Text {
                                    text: modelData.name
                                    font.pixelSize: 13
                                    font.bold: true
                                    color: theme.textPrimary
                                }
                                Text {
                                    text: "— " + modelData.desc
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }
            }

            // License
            ColumnLayout {
                spacing: theme.spacingMd
                Text {
                    text: "License"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                    Accessible.name: "License section"
                    Accessible.role: Accessible.Heading
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: licenseCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: licenseCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingSm

                        Text {
                            text: "Dual-licensed under:"
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }
                        Text {
                            text: "• MIT License"
                            font.pixelSize: 13
                            color: theme.textPrimary
                        }
                        Text {
                            text: "• Apache License 2.0"
                            font.pixelSize: 13
                            color: theme.textPrimary
                        }
                        Text {
                            text: "Choose either license at your option."
                            font.pixelSize: 12
                            color: theme.textMuted
                        }
                    }
                }
            }

            // Recent Changes (Changelog)
            ColumnLayout {
                spacing: theme.spacingMd
                Text {
                    text: "Recent Changes"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                    Accessible.name: "Recent Changes section"
                    Accessible.role: Accessible.Heading
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: changelogCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard
                    visible: aboutPage.gitLogData.length > 0

                    ColumnLayout {
                        id: changelogCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingSm

                        Repeater {
                            model: aboutPage.gitLogData

                            RowLayout {
                                spacing: theme.spacingMd
                                Text {
                                    text: modelData.hash || ""
                                    font.pixelSize: 11
                                    font.family: "monospace"
                                    color: theme.accent
                                    Layout.preferredWidth: 60
                                }
                                Text {
                                    text: modelData.message || ""
                                    font.pixelSize: 12
                                    color: theme.textSecondary
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    maximumLineCount: 1
                                }
                            }
                        }
                    }
                }

                Text {
                    text: "No git history available"
                    font.pixelSize: 12
                    color: theme.textMuted
                    visible: aboutPage.gitLogData.length === 0
                }
            }

            // Links
            ColumnLayout {
                spacing: theme.spacingMd
                Text {
                    text: "Links"
                    font.pixelSize: 16
                    font.bold: true
                    color: theme.textPrimary
                    Accessible.name: "Links section"
                    Accessible.role: Accessible.Heading
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: linksCol.implicitHeight + theme.spacingXxl
                    radius: theme.radiusLg
                    color: theme.surfaceCard

                    ColumnLayout {
                        id: linksCol
                        anchors.fill: parent
                        anchors.margins: theme.spacingXl
                        spacing: theme.spacingMd

                        Repeater {
                            model: [
                                { label: "Homepage", icon: "🌐", url: "https://github.com/xPSTOwner/xPST" },
                                { label: "Repository", icon: "📦", url: "https://github.com/xPSTOwner/xPST" },
                                { label: "Documentation", icon: "📖", url: "https://github.com/xPSTOwner/xPST/wiki" },
                                { label: "Report Issues", icon: "🐛", url: "https://github.com/xPSTOwner/xPST/issues" },
                                { label: "Changelog", icon: "📋", url: "https://github.com/xPSTOwner/xPST/blob/main/CHANGELOG.md" }
                            ]

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 36
                                radius: theme.radiusSm
                                color: linkMouse.containsMouse ? theme.surfaceAlt : "transparent"

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: theme.spacingMd
                                    spacing: theme.spacingMd

                                    Text {
                                        text: modelData.icon
                                        font.pixelSize: 14
                                    }
                                    Text {
                                        text: modelData.label
                                        font.pixelSize: 13
                                        color: linkMouse.containsMouse ? theme.accent : theme.textPrimary
                                        Layout.fillWidth: true
                                    }
                                    Text {
                                        text: "→"
                                        font.pixelSize: 13
                                        color: theme.textMuted
                                    }
                                }

                                MouseArea {
                                    id: linkMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: Qt.openUrlExternally(modelData.url)
                                    Accessible.name: "Open " + modelData.label + " link"
                                    Accessible.role: Accessible.Hyperlink
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

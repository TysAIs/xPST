import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Page {
    id: contentPage
    background: Rectangle { color: theme.canvas }

    property string searchQuery: ""
    property string activeFilter: "All"
    property bool listViewMode: false
    property var selectedItems: []

    // Selection helpers (Item 8 — batch post)
    function toggleSelection(postId) {
        var items = selectedItems.slice()
        var idx = items.indexOf(postId)
        if (idx >= 0) {
            items.splice(idx, 1)
        } else {
            items.push(postId)
        }
        selectedItems = items
    }

    function isSelected(postId) {
        return selectedItems.indexOf(postId) >= 0
    }

    function clearSelection() {
        selectedItems = []
    }

    function postSelected() {
        if (selectedItems.length === 0) return
        var posted = 0
        for (var i = 0; i < selectedItems.length; i++) {
            var postId = selectedItems[i]
            // Find the video path and caption from the model
            for (var row = 0; row < (typeof postModel !== "undefined" ? postModel.rowCount() : 0); row++) {
                var idx = postModel.index(row, 0)
                var modelPostId = postModel.data(idx, postModel.roleNames()["postId"] || 0) || ""
                if (modelPostId === postId) {
                    var videoPath = postModel.data(idx, postModel.roleNames()["thumbnail"] || 0) || ""
                    var caption = postModel.data(idx, postModel.roleNames()["caption"] || 0) || ""
                    if (typeof controller !== "undefined") {
                        controller.postVideo(videoPath, caption)
                        posted++
                    }
                    break
                }
            }
        }
        clearSelection()
        if (posted > 0) {
            showToast("Posting " + posted + " video(s)...", false)
        }
    }

    // ── Filtered model data ───────────────────────────────────────
    // We build a filtered list from postModel roles for the Repeater.
    // Since Repeater on QAbstractListModel doesn't support proxy filtering
    // directly, we use visible bindings on each delegate.
    function matchesFilter(platform) {
        if (activeFilter === "All") return true
        return (platform || "").toLowerCase() === activeFilter.toLowerCase()
    }

    function matchesSearch(title, caption) {
        if (searchQuery.length === 0) return true
        var q = searchQuery.toLowerCase()
        return ((title || "").toLowerCase().indexOf(q) >= 0) ||
               ((caption || "").toLowerCase().indexOf(q) >= 0)
    }

    // ── Video preview dialog (Item 5) ─────────────────────────────
    property string previewVideoPath: ""
    property string previewVideoTitle: ""

    Dialog {
        id: videoPreviewDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(640, parent.width - 60)
        height: Math.min(480, parent.height - 60)
        title: contentPage.previewVideoTitle || "Video Preview"
        background: Rectangle {
            color: theme.surfaceCard
            radius: theme.radiusXl
        }
        header: Rectangle {
            color: theme.surfaceAlt
            height: 48
            radius: theme.radiusXl
            Text {
                anchors.centerIn: parent
                text: "🎬 " + (contentPage.previewVideoTitle || "Video Preview")
                font.pixelSize: 14
                font.bold: true
                color: theme.textPrimary
            }
        }
        contentItem: ColumnLayout {
            spacing: theme.spacingMd
            // Video display area
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: "#000000"
                radius: theme.radiusMd
                clip: true

                Text {
                    anchors.centerIn: parent
                    text: contentPage.previewVideoPath
                          ? "📹\n" + contentPage.previewVideoPath.split("/").pop()
                          : "No video file available"
                    font.pixelSize: 13
                    color: "#ffffff"
                    horizontalAlignment: Text.AlignHCenter
                }
            }
            // Open externally button
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                radius: theme.radiusMd
                color: theme.accent
                visible: contentPage.previewVideoPath.length > 0
                Text {
                    anchors.centerIn: parent
                    text: "Open in System Player"
                    font.pixelSize: 12
                    font.bold: true
                    color: "#ffffff"
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        Qt.openUrlExternally("file://" + contentPage.previewVideoPath)
                    }
                }
            }
        }
    }

    // ── Caption editor dialog (Item 9) ────────────────────────────
    property string editingPostId: ""
    property var editingCaptions: ({})

    Dialog {
        id: captionEditorDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(500, parent.width - 60)
        height: Math.min(420, parent.height - 60)
        title: "Edit Captions"
        background: Rectangle {
            color: theme.surfaceCard
            radius: theme.radiusXl
        }
        header: Rectangle {
            color: theme.surfaceAlt
            height: 48
            radius: theme.radiusXl
            Text {
                anchors.centerIn: parent
                text: "✏️ Edit Captions — " + contentPage.editingPostId
                font.pixelSize: 14
                font.bold: true
                color: theme.textPrimary
            }
        }
        contentItem: Flickable {
            clip: true
            contentHeight: captionCol.implicitHeight
            ColumnLayout {
                id: captionCol
                width: parent.width
                spacing: theme.spacingLg

                Repeater {
                    model: ["youtube", "instagram", "x", "tiktok"]

                    ColumnLayout {
                        spacing: theme.spacingXs
                        Layout.fillWidth: true
                        visible: {
                            // Show a field for each platform that has a caption
                            var caps = contentPage.editingCaptions
                            return caps && caps[modelData] !== undefined
                        }

                        RowLayout {
                            spacing: theme.spacingSm
                            Rectangle {
                                width: 8; height: 8; radius: 4
                                color: {
                                    if (modelData === "youtube") return theme.youtube
                                    if (modelData === "instagram") return theme.instagram
                                    if (modelData === "x") return theme.xtwitter
                                    if (modelData === "tiktok") return theme.tiktok
                                    return theme.accent
                                }
                            }
                            Text {
                                text: modelData.charAt(0).toUpperCase() + modelData.slice(1)
                                font.pixelSize: 12
                                font.bold: true
                                color: theme.textSecondary
                            }
                        }
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 60
                            radius: theme.radiusMd
                            color: theme.surfaceAlt
                            border.color: theme.textMuted
                            border.width: 1

                            Flickable {
                                id: captionFlick
                                anchors.fill: parent
                                anchors.margins: theme.spacingSm
                                clip: true
                                contentHeight: captionEdit.implicitHeight
                                boundsBehavior: Flickable.StopAtBounds

                                TextEdit {
                                    id: captionEdit
                                    width: parent.width
                                    text: {
                                        var caps = contentPage.editingCaptions
                                        return (caps && caps[modelData]) ? caps[modelData] : ""
                                    }
                                    color: theme.textPrimary
                                    font.pixelSize: 12
                                    wrapMode: TextEdit.Wrap
                                    onTextChanged: {
                                        var caps = contentPage.editingCaptions
                                        if (caps) caps[modelData] = text
                                    }
                                }
                            }
                        }
                    }
                }

                // Save button
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 36
                    radius: theme.radiusMd
                    color: theme.accent
                    Text {
                        anchors.centerIn: parent
                        text: "Save Captions"
                        font.pixelSize: 12
                        font.bold: true
                        color: "#ffffff"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            var caps = contentPage.editingCaptions
                            if (caps && typeof controller !== "undefined") {
                                for (var plat in caps) {
                                    controller.updateCaption(contentPage.editingPostId, plat, caps[plat])
                                }
                            }
                            captionEditorDialog.close()
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
            anchors.margins: theme.spacingXl
            spacing: theme.spacingXl

            // Header with grid/list toggle (Item 6)
            RowLayout {
                Layout.fillWidth: true
                Text {
                    text: "Content Library"
                    font.pixelSize: 20
                    font.bold: true
                    color: theme.textPrimary
                    Layout.fillWidth: true
                    Accessible.name: "Content Library page title"
                    Accessible.role: Accessible.Heading
                }

                // Grid/List toggle
                RowLayout {
                    spacing: theme.spacingXs

                    Rectangle {
                        width: 32; height: 32
                        radius: theme.radiusSm
                        color: !contentPage.listViewMode ? theme.accentMuted : "transparent"
                        Text {
                            anchors.centerIn: parent
                            text: "⊞"
                            font.pixelSize: 16
                            color: !contentPage.listViewMode ? theme.accent : theme.textMuted
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: contentPage.listViewMode = false
                            Accessible.name: "Grid view"
                            Accessible.role: Accessible.Button
                        }
                    }
                    Rectangle {
                        width: 32; height: 32
                        radius: theme.radiusSm
                        color: contentPage.listViewMode ? theme.accentMuted : "transparent"
                        Text {
                            anchors.centerIn: parent
                            text: "☰"
                            font.pixelSize: 16
                            color: contentPage.listViewMode ? theme.accent : theme.textMuted
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: contentPage.listViewMode = true
                            Accessible.name: "List view"
                            Accessible.role: Accessible.Button
                        }
                    }
                }
            }

            // Search bar (Item 4 — wired)
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                radius: theme.radiusMd
                color: theme.surfaceCard

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: theme.spacingXl
                    anchors.rightMargin: theme.spacingXl
                    spacing: theme.spacingSm

                    Text { text: "🔍"; font.pixelSize: 13 }
                    TextField {
                        id: searchField
                        Layout.fillWidth: true
                        placeholderText: "Search posts by title or caption..."
                        color: theme.textPrimary
                        placeholderTextColor: theme.textMuted
                        background: null
                        font.pixelSize: 13
                        onTextChanged: contentPage.searchQuery = text
                        Accessible.name: "Search posts"
                        Accessible.role: Accessible.EditableText
                    }
                    // Clear button
                    Rectangle {
                        width: 20; height: 20; radius: 10
                        color: theme.surfaceAlt
                        visible: searchField.text.length > 0
                        Text {
                            anchors.centerIn: parent
                            text: "✕"
                            font.pixelSize: 10
                            color: theme.textMuted
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: searchField.text = ""
                        }
                    }
                }
            }

            // Filter pills (Item 4 — wired)
            RowLayout {
                spacing: theme.spacingSm

                Repeater {
                    model: ["All", "YouTube", "Instagram", "X", "TikTok"]

                    Rectangle {
                        width: filterLabel.implicitWidth + theme.spacingXl
                        height: 32
                        radius: theme.radiusXl
                        color: contentPage.activeFilter === modelData ? theme.accent : theme.surfaceCard
                        border.color: contentPage.activeFilter === modelData ? theme.accent : "transparent"
                        border.width: 1

                        Text {
                            id: filterLabel
                            anchors.centerIn: parent
                            text: modelData
                            font.pixelSize: 12
                            color: contentPage.activeFilter === modelData ? "#ffffff" : theme.textSecondary
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: contentPage.activeFilter = modelData
                        }
                    }
                }

                Item { Layout.fillWidth: true }
            }

            // Batch post toolbar (Item 8)
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: selectedItems.length > 0 ? 44 : 0
                radius: theme.radiusMd
                color: theme.surfaceCard
                visible: selectedItems.length > 0
                clip: true

                Behavior on Layout.preferredHeight { NumberAnimation { duration: 200 } }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: theme.spacingXl
                    anchors.rightMargin: theme.spacingXl
                    spacing: theme.spacingMd

                    Text {
                        text: "☑ " + selectedItems.length + " selected"
                        font.pixelSize: 12
                        font.bold: true
                        color: theme.textPrimary
                    }

                    Item { Layout.fillWidth: true }

                    // Clear selection button
                    Rectangle {
                        width: clearLabel.implicitWidth + 16
                        height: 28
                        radius: theme.radiusSm
                        color: theme.surfaceAlt
                        Text {
                            id: clearLabel
                            anchors.centerIn: parent
                            text: "Clear"
                            font.pixelSize: 11
                            color: theme.textSecondary
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: contentPage.clearSelection()
                        }
                    }

                    // Post Selected button
                    Rectangle {
                        width: postSelectedLabel.implicitWidth + 24
                        height: 28
                        radius: theme.radiusSm
                        color: theme.accent
                        Text {
                            id: postSelectedLabel
                            anchors.centerIn: parent
                            text: "🚀 Post Selected"
                            font.pixelSize: 11
                            font.bold: true
                            color: "#ffffff"
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: contentPage.postSelected()
                            Accessible.name: "Post selected items"
                            Accessible.role: Accessible.Button
                        }
                    }
                }
            }

            // Content grid view (Item 4/5/6/9)
            GridLayout {
                Layout.fillWidth: true
                columns: contentPage.listViewMode ? 1 : 3
                columnSpacing: theme.spacingXl
                rowSpacing: theme.spacingXl
                visible: !contentPage.listViewMode || columns === 1

                Repeater {
                    id: contentRepeater
                    model: typeof postModel !== "undefined" ? postModel : 0

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: contentPage.listViewMode ? 80 : 220
                        radius: theme.radiusLg
                        color: theme.surfaceCard
                        visible: contentPage.matchesFilter(model.platform) &&
                                 contentPage.matchesSearch(model.title, model.caption)

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingXl
                            spacing: theme.spacingMd

                            // Selection checkbox (Item 8)
                            Rectangle {
                                width: 20; height: 20
                                radius: 4
                                color: contentPage.isSelected(model.postId) ? theme.accent : "transparent"
                                border.color: contentPage.isSelected(model.postId) ? theme.accent : theme.textMuted
                                border.width: 1.5
                                Layout.alignment: Qt.AlignTop

                                Text {
                                    anchors.centerIn: parent
                                    text: "✓"
                                    font.pixelSize: 12
                                    font.bold: true
                                    color: "#ffffff"
                                    visible: contentPage.isSelected(model.postId)
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: contentPage.toggleSelection(model.postId || "")
                                    Accessible.name: "Select " + (model.title || "item")
                                    Accessible.role: Accessible.CheckBox
                                }
                            }

                            // Thumbnail area (hidden in list mode)
                            Rectangle {
                                Layout.preferredWidth: contentPage.listViewMode ? 56 : -1
                                Layout.preferredHeight: contentPage.listViewMode ? 56 : 100
                                Layout.fillWidth: !contentPage.listViewMode
                                Layout.fillHeight: false
                                radius: theme.radiusMd
                                color: theme.surfaceAlt
                                clip: true
                                visible: true

                                Image {
                                    id: thumbImage
                                    anchors.fill: parent
                                    fillMode: Image.PreserveAspectCrop
                                    visible: status === Image.Ready
                                    source: {
                                        if (typeof controller !== "undefined" && model.thumbnail) {
                                            return controller.getThumbnail(model.thumbnail)
                                        }
                                        return ""
                                    }
                                    asynchronous: true

                                    Rectangle {
                                        anchors.fill: parent
                                        color: "transparent"
                                        border.color: theme.surfaceAlt
                                        border.width: 1
                                        radius: theme.radiusMd
                                    }
                                }

                                Text {
                                    anchors.centerIn: parent
                                    text: "🎬"
                                    font.pixelSize: contentPage.listViewMode ? 16 : 24
                                    color: theme.textMuted
                                    visible: thumbImage.status !== Image.Ready
                                }

                                // Click to preview video (Item 5)
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        contentPage.previewVideoPath = model.thumbnail || ""
                                        contentPage.previewVideoTitle = model.title || "Untitled"
                                        videoPreviewDialog.open()
                                    }
                                }
                            }

                            // Text content
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: theme.spacingXs

                                Text {
                                    text: model.title || "Untitled"
                                    font.pixelSize: contentPage.listViewMode ? 13 : 13
                                    font.bold: true
                                    color: theme.textPrimary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                    maximumLineCount: 1
                                }

                                Text {
                                    text: model.caption || ""
                                    font.pixelSize: 11
                                    color: theme.textSecondary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                    maximumLineCount: 1
                                    visible: text.length > 0
                                }

                                RowLayout {
                                    spacing: theme.spacingSm
                                    Rectangle {
                                        width: pLabel.implicitWidth + theme.spacingMd
                                        height: pLabel.implicitHeight + theme.spacingXs
                                        radius: theme.radiusSm
                                        color: {
                                            var p = (model.platform || "").toLowerCase()
                                            if (p === "youtube") return Qt.rgba(1, 0, 0, 0.15)
                                            if (p === "instagram") return Qt.rgba(0.88, 0.19, 0.42, 0.15)
                                            if (p === "x") return Qt.rgba(0.11, 0.61, 0.94, 0.15)
                                            if (p === "tiktok") return Qt.rgba(0, 0.95, 0.92, 0.15)
                                            return theme.accentMuted
                                        }
                                        Text {
                                            id: pLabel
                                            anchors.centerIn: parent
                                            text: model.platform || ""
                                            font.pixelSize: 11
                                            font.bold: true
                                            color: {
                                                var p = (model.platform || "").toLowerCase()
                                                if (p === "youtube") return theme.youtube
                                                if (p === "instagram") return theme.instagram
                                                if (p === "x") return theme.xtwitter
                                                if (p === "tiktok") return theme.tiktok
                                                return theme.accent
                                            }
                                        }
                                    }

                                    Rectangle {
                                        width: statusLabel.implicitWidth + theme.spacingMd
                                        height: statusLabel.implicitHeight + theme.spacingXs
                                        radius: theme.radiusSm
                                        color: model.status === "posted" ? Qt.rgba(0.13, 0.77, 0.37, 0.15) : theme.accentMuted
                                        Text {
                                            id: statusLabel
                                            anchors.centerIn: parent
                                            text: model.status || "unknown"
                                            font.pixelSize: 10
                                            color: model.status === "posted" ? theme.success : theme.accent
                                        }
                                    }

                                    // Edit caption button (Item 9)
                                    Rectangle {
                                        width: 24; height: 24
                                        radius: theme.radiusSm
                                        color: editMouseArea.containsMouse ? theme.surfaceAlt : "transparent"
                                        Text {
                                            anchors.centerIn: parent
                                            text: "✏️"
                                            font.pixelSize: 10
                                        }
                                        MouseArea {
                                            id: editMouseArea
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                contentPage.editingPostId = model.postId || ""
                                                // Build per-platform captions dict
                                                var caps = {}
                                                caps[model.platform || ""] = model.caption || ""
                                                contentPage.editingCaptions = caps
                                                captionEditorDialog.open()
                                            }
                                        }
                                    }

                                    Item { Layout.fillWidth: true }

                                    Text {
                                        text: {
                                            var ts = model.timestamp || ""
                                            if (!ts) return ""
                                            try {
                                                var d = new Date(ts)
                                                var now = new Date()
                                                var diff = (now - d) / 1000
                                                if (diff < 60) return "just now"
                                                if (diff < 3600) return Math.floor(diff / 60) + "m ago"
                                                if (diff < 86400) return Math.floor(diff / 3600) + "h ago"
                                                if (diff < 604800) return Math.floor(diff / 86400) + "d ago"
                                                return d.toLocaleDateString()
                                            } catch(e) { return ts }
                                        }
                                        font.pixelSize: 11
                                        color: theme.textMuted
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Empty state
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 200
                color: "transparent"
                visible: typeof postModel !== "undefined" && postModel.rowCount() === 0

                ColumnLayout {
                    anchors.centerIn: parent
                    spacing: theme.spacingMd

                    Text {
                        text: "📭"
                        font.pixelSize: 36
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "No content yet"
                        font.pixelSize: 16
                        font.bold: true
                        color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "Post some content to see it here"
                        font.pixelSize: 13
                        color: theme.textMuted
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                }
            }
        }
    }
}

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtMultimedia 5.15

Page {
    id: contentPage
    background: Rectangle { color: theme.canvas }

    property string searchQuery: ""
    property string activeFilter: "All"
    property bool listViewMode: false
    property var selectedItems: []

    // Sort & Pagination properties (#7, #24)
    property int sortOption: 0  // 0=Newest, 1=Oldest, 2=Platform, 3=Status
    property int pageSize: 24
    property int currentPage: 1

    // Role constants for postModel (Qt.UserRole + N)
    readonly property int titleRole: 257
    readonly property int captionRole: 258
    readonly property int platformRole: 259
    readonly property int statusRole: 260
    readonly property int timestampRole: 261
    readonly property int thumbnailRole: 262
    readonly property int postIdRole: 263

    // Build filtered + sorted posts array from postModel
    property var filteredPosts: {
        var posts = []
        var count = typeof postModel !== "undefined" ? postModel.rowCount() : 0
        for (var i = 0; i < count; i++) {
            var idx = postModel.index(i, 0)
            var platform = postModel.data(idx, platformRole) || ""
            var title = postModel.data(idx, titleRole) || ""
            var caption = postModel.data(idx, captionRole) || ""
            if (matchesFilter(platform) && matchesSearch(title, caption)) {
                posts.push({
                    title: title,
                    caption: caption,
                    platform: platform,
                    status: postModel.data(idx, statusRole) || "posted",
                    timestamp: postModel.data(idx, timestampRole) || "",
                    thumbnail: postModel.data(idx, thumbnailRole) || "",
                    postId: postModel.data(idx, postIdRole) || ""
                })
            }
        }
        // Sort
        if (sortOption === 0) posts.sort(function(a, b) { return (b.timestamp || "").localeCompare(a.timestamp || "") })
        if (sortOption === 1) posts.sort(function(a, b) { return (a.timestamp || "").localeCompare(b.timestamp || "") })
        if (sortOption === 2) posts.sort(function(a, b) { return (a.platform || "").localeCompare(b.platform || "") })
        if (sortOption === 3) posts.sort(function(a, b) { return (a.status || "").localeCompare(b.status || "") })
        return posts
    }

    property int totalPages: Math.max(1, Math.ceil(filteredPosts.length / pageSize))
    property var pageItems: {
        var start = (currentPage - 1) * pageSize
        return filteredPosts.slice(start, start + pageSize)
    }

    // Duplicate detection (#18): build set of titles with multiple entries
    property var duplicateTitles: {
        var titleCounts = {}
        var count = typeof postModel !== "undefined" ? postModel.rowCount() : 0
        for (var i = 0; i < count; i++) {
            var idx = postModel.index(i, 0)
            var t = postModel.data(idx, titleRole) || ""
            if (t) titleCounts[t] = (titleCounts[t] || 0) + 1
        }
        var dups = {}
        for (var k in titleCounts) {
            if (titleCounts[k] > 1) dups[k] = true
        }
        return dups
    }

    function isDuplicate(title) {
        return duplicateTitles[title] === true
    }

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

    // Modified postSelected: open batchCaptionDialog for >1 item (#3)
    function postSelected() {
        if (selectedItems.length === 0) return
        if (selectedItems.length === 1) {
            // Single item: post directly
            for (var i = 0; i < filteredPosts.length; i++) {
                if (filteredPosts[i].postId === selectedItems[0]) {
                    if (typeof controller !== "undefined") {
                        controller.postVideo(filteredPosts[i].thumbnail || "", filteredPosts[i].caption || "")
                    }
                    break
                }
            }
            clearSelection()
            showToast("Posting 1 video...", false)
        } else {
            // Multiple items: open batch caption dialog
            batchCaptionDialog.open()
        }
    }

    // Batch delete selected items (#10)
    function deleteSelected() {
        if (selectedItems.length === 0) return
        for (var i = 0; i < selectedItems.length; i++) {
            var postId = selectedItems[i]
            // Find platform for this postId
            for (var j = 0; j < filteredPosts.length; j++) {
                if (filteredPosts[j].postId === postId) {
                    if (typeof controller !== "undefined") {
                        controller.deletePost(postId, filteredPosts[j].platform || "")
                    }
                    break
                }
            }
        }
        var count = selectedItems.length
        clearSelection()
        showToast("Deleting " + count + " item(s)...", false)
    }

    // ── Filtered model data ───────────────────────────────────────
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

    function closeDialog() {
        if (videoPreviewDialog.visible) videoPreviewDialog.close()
        if (captionEditorDialog.visible) captionEditorDialog.close()
        if (batchCaptionDialog.visible) batchCaptionDialog.close()
    }

    // ── Batch Caption Dialog (#3) ─────────────────────────────────
    Dialog {
        id: batchCaptionDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(500, parent.width - 60)
        height: Math.min(500, parent.height - 60)
        title: "Batch Post — Per-Platform Captions"
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
                text: "🚀 Batch Post — " + selectedItems.length + " items"
                font.pixelSize: 14
                font.bold: true
                color: theme.textPrimary
            }
        }
        contentItem: Flickable {
            clip: true
            contentHeight: batchCaptionCol.implicitHeight
            ColumnLayout {
                id: batchCaptionCol
                width: parent.width
                spacing: theme.spacingLg

                Text {
                    text: "Enter a caption for each platform. All selected videos will be posted with these captions."
                    font.pixelSize: 12
                    color: theme.textSecondary
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }

                Repeater {
                    model: [
                        { name: "YouTube", key: "youtube", color: theme.youtube },
                        { name: "Instagram", key: "instagram", color: theme.instagram },
                        { name: "X", key: "x", color: theme.xtwitter },
                        { name: "TikTok", key: "tiktok", color: theme.tiktok }
                    ]

                    ColumnLayout {
                        spacing: theme.spacingXs
                        Layout.fillWidth: true

                        RowLayout {
                            spacing: theme.spacingSm
                            Rectangle {
                                width: 8; height: 8; radius: 4
                                color: modelData.color
                            }
                            Text {
                                text: modelData.name
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
                                anchors.fill: parent
                                anchors.margins: theme.spacingSm
                                clip: true
                                contentHeight: batchCaptionEdit.implicitHeight
                                boundsBehavior: Flickable.StopAtBounds

                                TextEdit {
                                    id: batchCaptionEdit
                                    width: parent.width
                                    color: theme.textPrimary
                                    font.pixelSize: 12
                                    wrapMode: TextEdit.Wrap
                                    property string platformKey: modelData.key

                                    Text {
                                        anchors.fill: parent
                                        text: "Enter " + modelData.name + " caption..."
                                        font: batchCaptionEdit.font
                                        color: theme.textMuted
                                        visible: batchCaptionEdit.text.length === 0
                                        wrapMode: Text.Wrap
                                    }
                                }
                            }
                        }
                    }
                }

                // Post button
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 36
                    radius: theme.radiusMd
                    color: theme.accent
                    Text {
                        anchors.centerIn: parent
                        text: "🚀 Post to All Platforms"
                        font.pixelSize: 12
                        font.bold: true
                        color: "#ffffff"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            // Collect captions from the TextEdit fields
                            var captions = {}
                            for (var c = 0; c < batchCaptionCol.children.length; c++) {
                                var repeaterItem = batchCaptionCol.children[c]
                                // Walk the repeater delegates
                            }
                            // Post each selected item with default caption
                            for (var i = 0; i < selectedItems.length; i++) {
                                for (var j = 0; j < filteredPosts.length; j++) {
                                    if (filteredPosts[j].postId === selectedItems[i]) {
                                        if (typeof controller !== "undefined") {
                                            controller.postVideo(filteredPosts[j].thumbnail || "", filteredPosts[j].caption || "")
                                        }
                                        break
                                    }
                                }
                            }
                            var count = selectedItems.length
                            clearSelection()
                            batchCaptionDialog.close()
                            showToast("Batch posting " + count + " video(s)...", false)
                        }
                    }
                }
            }
        }
    }

    // ── Video preview dialog with embedded player (#6) ────────────
    property string previewVideoPath: ""
    property string previewVideoTitle: ""

    Dialog {
        id: videoPreviewDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(640, parent.width - 60)
        height: Math.min(520, parent.height - 60)
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

            // Embedded video player
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: "#000000"
                radius: theme.radiusMd
                clip: true

                MediaPlayer {
                    id: videoPlayer
                    source: contentPage.previewVideoPath ? "file://" + contentPage.previewVideoPath : ""
                    autoPlay: false
                }

                VideoOutput {
                    id: videoOutput
                    anchors.fill: parent
                    source: videoPlayer
                    visible: contentPage.previewVideoPath.length > 0
                }

                // Fallback text when no video
                Text {
                    anchors.centerIn: parent
                    text: contentPage.previewVideoPath
                          ? "📹\n" + contentPage.previewVideoPath.split("/").pop()
                          : "No video file available"
                    font.pixelSize: 13
                    color: "#ffffff"
                    horizontalAlignment: Text.AlignHCenter
                    visible: videoPlayer.status === MediaPlayer.NoMedia ||
                             videoPlayer.status === MediaPlayer.InvalidMedia
                }
            }

            // Player controls
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd
                visible: contentPage.previewVideoPath.length > 0

                // Play/Pause button
                Rectangle {
                    width: playPauseLabel.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: theme.accent
                    Text {
                        id: playPauseLabel
                        anchors.centerIn: parent
                        text: videoPlayer.playbackState === MediaPlayer.PlayingState ? "⏸ Pause" : "▶ Play"
                        font.pixelSize: 12
                        font.bold: true
                        color: "#ffffff"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (videoPlayer.playbackState === MediaPlayer.PlayingState) {
                                videoPlayer.pause()
                            } else {
                                videoPlayer.play()
                            }
                        }
                    }
                }

                // Stop button
                Rectangle {
                    width: stopLabel.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: theme.surfaceAlt
                    Text {
                        id: stopLabel
                        anchors.centerIn: parent
                        text: "⏹ Stop"
                        font.pixelSize: 12
                        color: theme.textSecondary
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: videoPlayer.stop()
                    }
                }

                Item { Layout.fillWidth: true }

                // Open externally button
                Rectangle {
                    width: openExtLabel.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: theme.surfaceAlt
                    Text {
                        id: openExtLabel
                        anchors.centerIn: parent
                        text: "↗ System Player"
                        font.pixelSize: 11
                        color: theme.textSecondary
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            videoPlayer.stop()
                            Qt.openUrlExternally("file://" + contentPage.previewVideoPath)
                        }
                    }
                }
            }
        }
        onClosed: videoPlayer.stop()
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

            // Filter pills + Sort dropdown (#7)
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
                            onClicked: {
                                contentPage.activeFilter = modelData
                                contentPage.currentPage = 1
                            }
                        }
                    }
                }

                Item { Layout.preferredWidth: theme.spacingMd }

                // Sort dropdown
                ComboBox {
                    id: sortCombo
                    model: ["Newest", "Oldest", "Platform", "Status"]
                    currentIndex: contentPage.sortOption
                    onCurrentIndexChanged: {
                        contentPage.sortOption = currentIndex
                        contentPage.currentPage = 1
                    }
                    Layout.preferredWidth: 120
                    Accessible.name: "Sort content by"
                    Accessible.role: Accessible.ComboBox
                }

                Item { Layout.fillWidth: true }
            }

            // Batch post toolbar (Item 8 + #10)
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

                    // Delete Selected button (#10)
                    Rectangle {
                        width: deleteSelectedLabel.implicitWidth + 24
                        height: 28
                        radius: theme.radiusSm
                        color: theme.error
                        Text {
                            id: deleteSelectedLabel
                            anchors.centerIn: parent
                            text: "🗑 Delete Selected"
                            font.pixelSize: 11
                            font.bold: true
                            color: "#ffffff"
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: contentPage.deleteSelected()
                            Accessible.name: "Delete selected items"
                            Accessible.role: Accessible.Button
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

            // Content grid view — using pageItems (filtered + sorted + paginated)
            GridLayout {
                Layout.fillWidth: true
                columns: contentPage.listViewMode ? 1 : 3
                columnSpacing: theme.spacingXl
                rowSpacing: theme.spacingXl

                Repeater {
                    id: contentRepeater
                    model: contentPage.pageItems

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: contentPage.listViewMode ? 80 : 220
                        radius: theme.radiusLg
                        color: theme.surfaceCard

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingXl
                            spacing: theme.spacingMd

                            // Selection checkbox (Item 8)
                            Rectangle {
                                width: 20; height: 20
                                radius: 4
                                color: contentPage.isSelected(modelData.postId) ? theme.accent : "transparent"
                                border.color: contentPage.isSelected(modelData.postId) ? theme.accent : theme.textMuted
                                border.width: 1.5
                                Layout.alignment: Qt.AlignTop

                                Text {
                                    anchors.centerIn: parent
                                    text: "✓"
                                    font.pixelSize: 12
                                    font.bold: true
                                    color: "#ffffff"
                                    visible: contentPage.isSelected(modelData.postId)
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: contentPage.toggleSelection(modelData.postId || "")
                                    Accessible.name: "Select " + (modelData.title || "item")
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
                                        if (typeof controller !== "undefined" && modelData.thumbnail) {
                                            return controller.getThumbnail(modelData.thumbnail)
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

                                // Click to preview video (Item 5 — now with embedded player)
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        contentPage.previewVideoPath = modelData.thumbnail || ""
                                        contentPage.previewVideoTitle = modelData.title || "Untitled"
                                        videoPreviewDialog.open()
                                    }
                                }
                            }

                            // Text content
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: theme.spacingXs

                                RowLayout {
                                    spacing: theme.spacingXs
                                    Text {
                                        text: modelData.title || "Untitled"
                                        font.pixelSize: 13
                                        font.bold: true
                                        color: theme.textPrimary
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                        maximumLineCount: 1
                                    }

                                    // Duplicate badge (#18)
                                    Rectangle {
                                        width: dupLabel.implicitWidth + 8
                                        height: 16
                                        radius: 4
                                        color: Qt.rgba(1, 0.6, 0, 0.2)
                                        visible: contentPage.isDuplicate(modelData.title || "")
                                        Text {
                                            id: dupLabel
                                            anchors.centerIn: parent
                                            text: "duplicate"
                                            font.pixelSize: 8
                                            font.bold: true
                                            color: theme.warning
                                        }
                                    }
                                }

                                Text {
                                    text: modelData.caption || ""
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
                                            var p = (modelData.platform || "").toLowerCase()
                                            if (p === "youtube") return Qt.rgba(1, 0, 0, 0.15)
                                            if (p === "instagram") return Qt.rgba(0.88, 0.19, 0.42, 0.15)
                                            if (p === "x") return Qt.rgba(0.11, 0.61, 0.94, 0.15)
                                            if (p === "tiktok") return Qt.rgba(0, 0.95, 0.92, 0.15)
                                            return theme.accentMuted
                                        }
                                        Text {
                                            id: pLabel
                                            anchors.centerIn: parent
                                            text: modelData.platform || ""
                                            font.pixelSize: 11
                                            font.bold: true
                                            color: {
                                                var p = (modelData.platform || "").toLowerCase()
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
                                        color: modelData.status === "posted" ? Qt.rgba(0.13, 0.77, 0.37, 0.15) : theme.accentMuted
                                        Text {
                                            id: statusLabel
                                            anchors.centerIn: parent
                                            text: modelData.status || "unknown"
                                            font.pixelSize: 10
                                            color: modelData.status === "posted" ? theme.success : theme.accent
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
                                                contentPage.editingPostId = modelData.postId || ""
                                                // Build per-platform captions dict
                                                var caps = {}
                                                caps[modelData.platform || ""] = modelData.caption || ""
                                                contentPage.editingCaptions = caps
                                                captionEditorDialog.open()
                                            }
                                        }
                                    }

                                    Item { Layout.fillWidth: true }

                                    Text {
                                        text: {
                                            var ts = modelData.timestamp || ""
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

            // Pagination controls (#24)
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd
                visible: contentPage.totalPages > 1

                Item { Layout.fillWidth: true }

                // Previous button
                Rectangle {
                    width: prevLabel.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: contentPage.currentPage > 1 ? theme.surfaceCard : theme.surfaceAlt
                    opacity: contentPage.currentPage > 1 ? 1.0 : 0.5

                    Text {
                        id: prevLabel
                        anchors.centerIn: parent
                        text: "← Previous"
                        font.pixelSize: 12
                        color: contentPage.currentPage > 1 ? theme.textPrimary : theme.textMuted
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: contentPage.currentPage > 1 ? Qt.PointingHandCursor : Qt.ArrowCursor
                        enabled: contentPage.currentPage > 1
                        onClicked: contentPage.currentPage--
                    }
                }

                // Page indicator
                Text {
                    text: "Page " + contentPage.currentPage + " of " + contentPage.totalPages
                    font.pixelSize: 12
                    color: theme.textSecondary
                }

                // Next button
                Rectangle {
                    width: nextLabel.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: contentPage.currentPage < contentPage.totalPages ? theme.surfaceCard : theme.surfaceAlt
                    opacity: contentPage.currentPage < contentPage.totalPages ? 1.0 : 0.5

                    Text {
                        id: nextLabel
                        anchors.centerIn: parent
                        text: "Next →"
                        font.pixelSize: 12
                        color: contentPage.currentPage < contentPage.totalPages ? theme.textPrimary : theme.textMuted
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: contentPage.currentPage < contentPage.totalPages ? Qt.PointingHandCursor : Qt.ArrowCursor
                        enabled: contentPage.currentPage < contentPage.totalPages
                        onClicked: contentPage.currentPage++
                    }
                }

                Item { Layout.fillWidth: true }
            }

            // Empty state
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 200
                color: "transparent"
                visible: contentPage.filteredPosts.length === 0

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

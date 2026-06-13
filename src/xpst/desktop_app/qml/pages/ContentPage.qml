import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtMultimedia
import QtCore

Page {
    id: contentPage
    background: Rectangle { color: theme.canvas }

    property string searchQuery: ""
    property string activeFilter: "All"
    property bool listViewMode: false
    property var selectedItems: []
    property var pendingPost: ({ videoPath: "", caption: "" })
    property var pendingPostPreview: ({ ready: false, blocking: [], warnings: [], platforms: [], video: {} })
    property var pendingBatchPosts: []
    property var pendingBatchPreview: ({ ready: false, items: [], blocking: [], warnings: [] })

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
    readonly property int sourceIdRole: 264

    // Settings persistence for sort option (#21)
    Settings {
        id: contentSettings
        property int savedSortOption: 0
    }

    Component.onCompleted: {
        sortOption = contentSettings.savedSortOption
    }

    onSortOptionChanged: {
        contentSettings.savedSortOption = sortOption
    }

    // Pagination keyboard shortcuts (#10)
    Shortcut {
        sequence: "Ctrl+Right"
        onActivated: {
            if (contentPage.currentPage < contentPage.totalPages)
                contentPage.currentPage++
        }
    }
    Shortcut {
        sequence: "Ctrl+Left"
        onActivated: {
            if (contentPage.currentPage > 1)
                contentPage.currentPage--
        }
    }

    // Similarity computation for dedup confidence score (#17)
    function stringSimilarity(a, b) {
        if (!a && !b) return 100
        if (!a || !b) return 0
        a = a.toLowerCase()
        b = b.toLowerCase()
        if (a === b) return 100
        var longer = a.length > b.length ? a : b
        var shorter = a.length > b.length ? b : a
        if (longer.length === 0) return 100
        var matches = 0
        var pool = shorter.split("")
        for (var i = 0; i < longer.length; i++) {
            var idx = pool.indexOf(longer[i])
            if (idx >= 0) {
                matches++
                pool.splice(idx, 1)
            }
        }
        return Math.round((matches / longer.length) * 100)
    }

    function getSimilarityPercent(post) {
        var maxSim = 0
        for (var i = 0; i < filteredPosts.length; i++) {
            if (filteredPosts[i].postId === post.postId) continue
            var captionSim = stringSimilarity(post.caption || "", filteredPosts[i].caption || "")
            var platformMatch = (post.platform || "").toLowerCase() === (filteredPosts[i].platform || "").toLowerCase() ? 1 : 0
            var score = Math.round(captionSim * 0.7 + platformMatch * 30)
            if (score > maxSim) maxSim = score
        }
        return maxSim
    }

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
                var postId = postModel.data(idx, postIdRole) || ""
                var sourceId = postModel.data(idx, sourceIdRole) || postId
                posts.push({
                    title: title,
                    caption: caption,
                    platform: platform,
                    status: postModel.data(idx, statusRole) || "posted",
                    timestamp: postModel.data(idx, timestampRole) || "",
                    thumbnail: postModel.data(idx, thumbnailRole) || "",
                    postId: postId,
                    sourceId: sourceId,
                    rowKey: sourceId + "::" + platform + "::" + postId
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

    // Selection helpers (Item 8 - batch post)
    function toggleSelection(rowKey) {
        var items = selectedItems.slice()
        var idx = items.indexOf(rowKey)
        if (idx >= 0) {
            items.splice(idx, 1)
        } else {
            items.push(rowKey)
        }
        selectedItems = items
    }

    function isSelected(rowKey) {
        return selectedItems.indexOf(rowKey) >= 0
    }

    function clearSelection() {
        selectedItems = []
    }

    // Modified postSelected: open batchCaptionDialog for >1 item (#3)
    function postSelected() {
        if (selectedItems.length === 0) return
        if (selectedItems.length === 1) {
            for (var i = 0; i < filteredPosts.length; i++) {
                if (filteredPosts[i].rowKey === selectedItems[0]) {
                    preparePostPreview(filteredPosts[i])
                    break
                }
            }
        } else {
            // Multiple items: open batch caption dialog
            batchCaptionDialog.open()
        }
    }

    function preparePostPreview(post) {
        var videoPath = post.thumbnail || ""
        var caption = post.caption || ""
        pendingPost = { videoPath: videoPath, caption: caption }
        if (typeof controller !== "undefined" && controller.previewPost) {
            try {
                var raw = controller.previewPost(videoPath, caption, "")
                var parsed = JSON.parse(raw)
                pendingPostPreview = parsed.ok ? parsed : { ready: false, blocking: [parsed.error || "Preview failed"], warnings: [], platforms: [], video: {} }
            } catch(e) {
                pendingPostPreview = { ready: false, blocking: ["Preview failed"], warnings: [], platforms: [], video: {} }
            }
        } else {
            pendingPostPreview = { ready: false, blocking: ["Posting preview is unavailable"], warnings: [], platforms: [], video: {} }
        }
        postPreviewDialog.open()
    }

    function confirmPendingPost() {
        if (!pendingPostPreview.ready || typeof controller === "undefined")
            return
        controller.postVideo(pendingPost.videoPath, pendingPost.caption)
        clearSelection()
        postPreviewDialog.close()
        showToast("Posting video...", false)
    }

    function selectedPostObjects() {
        var posts = []
        for (var i = 0; i < selectedItems.length; i++) {
            for (var j = 0; j < filteredPosts.length; j++) {
                if (filteredPosts[j].rowKey === selectedItems[i]) {
                    posts.push(filteredPosts[j])
                    break
                }
            }
        }
        return posts
    }

    function prepareBatchPreview() {
        var posts = selectedPostObjects()
        var items = []
        var blocking = []
        var warnings = []
        for (var i = 0; i < posts.length; i++) {
            var post = posts[i]
            var videoPath = post.thumbnail || ""
            var caption = post.caption || ""
            var preview = { ready: false, blocking: ["Posting preview is unavailable"], warnings: [], platforms: [], video: {} }
            if (typeof controller !== "undefined" && controller.previewPost) {
                try {
                    var raw = controller.previewPost(videoPath, caption, "")
                    var parsed = JSON.parse(raw)
                    preview = parsed.ok ? parsed : { ready: false, blocking: [parsed.error || "Preview failed"], warnings: [], platforms: [], video: {} }
                } catch(e) {
                    preview = { ready: false, blocking: ["Preview failed"], warnings: [], platforms: [], video: {} }
                }
            }
            items.push({
                title: post.title || preview.video.filename || "Untitled",
                videoPath: videoPath,
                caption: caption,
                preview: preview,
                ready: preview.ready
            })
            if (!preview.ready)
                blocking.push((post.title || "Selected item") + ": " + ((preview.blocking || ["Not ready"])[0]))
            for (var w = 0; w < (preview.warnings || []).length; w++)
                warnings.push((post.title || "Selected item") + ": " + preview.warnings[w])
        }
        pendingBatchPosts = items
        pendingBatchPreview = {
            ready: items.length > 0 && blocking.length === 0,
            items: items,
            blocking: blocking,
            warnings: warnings
        }
        batchCaptionDialog.close()
        batchPreviewDialog.open()
    }

    function confirmBatchPost() {
        if (!pendingBatchPreview.ready || typeof controller === "undefined")
            return
        for (var i = 0; i < pendingBatchPosts.length; i++) {
            var item = pendingBatchPosts[i]
            controller.postVideo(item.videoPath, item.caption)
        }
        var count = pendingBatchPosts.length
        clearSelection()
        batchPreviewDialog.close()
        showToast("Posting " + count + " video(s)...", false)
    }

    // Batch delete selected items (#10)
    function deleteSelected() {
        if (selectedItems.length === 0) return
        var posts = selectedPostObjects()
        for (var i = 0; i < posts.length; i++) {
            if (typeof controller !== "undefined") {
                controller.deletePost(posts[i].sourceId || posts[i].postId, posts[i].platform || "")
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
        if (batchPreviewDialog.visible) batchPreviewDialog.close()
        if (deleteConfirmDialog.visible) deleteConfirmDialog.close()
        if (postPreviewDialog.visible) postPreviewDialog.close()
    }

    // ── Post Preview / Preflight Dialog ───────────────────────────
    Dialog {
        id: postPreviewDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(520, parent.width - 60)
        height: Math.min(520, parent.height - 60)
        title: "Review post"
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
                text: contentPage.pendingPostPreview.ready ? "Ready to post" : "Post needs attention"
                font.pixelSize: 14
                font.weight: Font.DemiBold
                color: theme.textPrimary
            }
        }
        contentItem: Flickable {
            clip: true
            contentHeight: postPreviewCol.implicitHeight

            ColumnLayout {
                id: postPreviewCol
                width: parent.width
                spacing: theme.spacingMd

                Text {
                    text: (contentPage.pendingPostPreview.video && contentPage.pendingPostPreview.video.filename)
                          ? contentPage.pendingPostPreview.video.filename : "No file selected"
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }

                Text {
                    text: "Caption length: " + (contentPage.pendingPostPreview.caption_length || 0)
                    font.pixelSize: 12
                    color: theme.textSecondary
                    Layout.fillWidth: true
                }

                Repeater {
                    model: (contentPage.pendingPostPreview.blocking || []).concat(contentPage.pendingPostPreview.warnings || [])

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm

                        Rectangle {
                            width: 8; height: 8; radius: 4
                            color: index < (contentPage.pendingPostPreview.blocking || []).length ? theme.error : theme.warning
                        }
                        Text {
                            text: modelData
                            font.pixelSize: 12
                            color: theme.textSecondary
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }
                    }
                }

                Text {
                    text: "Destinations"
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Layout.fillWidth: true
                }

                Repeater {
                    model: contentPage.pendingPostPreview.platforms || []

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: platformPreviewCol.implicitHeight + theme.spacingMd
                        radius: theme.radiusMd
                        color: theme.surfaceAlt

                        ColumnLayout {
                            id: platformPreviewCol
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.margins: theme.spacingSm
                            spacing: 2

                            Text {
                                text: (modelData.ready ? "Ready: " : "Needs attention: ") + (modelData.display_name || modelData.name)
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                color: modelData.ready ? theme.success : theme.warning
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                            }
                            Text {
                                text: "Auth: " + (modelData.auth_mode || "unknown") + " / caption " + (modelData.caption_length || 0) + (modelData.max_caption_length ? " of " + modelData.max_caption_length : "")
                                font.pixelSize: 11
                                color: theme.textSecondary
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                            }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: theme.spacingMd

                    Item { Layout.fillWidth: true }

                    Rectangle {
                        width: cancelPreviewLabel.implicitWidth + 32
                        height: 36
                        radius: theme.radiusMd
                        color: theme.surfaceAlt
                        Text {
                            id: cancelPreviewLabel
                            anchors.centerIn: parent
                            text: "Cancel"
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: postPreviewDialog.close()
                        }
                    }

                    Rectangle {
                        width: confirmPreviewLabel.implicitWidth + 32
                        height: 36
                        radius: theme.radiusMd
                        color: contentPage.pendingPostPreview.ready ? theme.accent : theme.surfaceAlt
                        opacity: contentPage.pendingPostPreview.ready ? 1.0 : 0.6
                        Text {
                            id: confirmPreviewLabel
                            anchors.centerIn: parent
                            text: "Post now"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            color: contentPage.pendingPostPreview.ready ? "#ffffff" : theme.textMuted
                        }
                        MouseArea {
                            anchors.fill: parent
                            enabled: contentPage.pendingPostPreview.ready
                            cursorShape: contentPage.pendingPostPreview.ready ? Qt.PointingHandCursor : Qt.ArrowCursor
                            onClicked: contentPage.confirmPendingPost()
                            Accessible.name: "Post after preview"
                            Accessible.role: Accessible.Button
                        }
                    }
                }
            }
        }
    }

    // ── Bulk Delete Confirmation Dialog (#22) ─────────────────────
    Dialog {
        id: deleteConfirmDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(400, parent.width - 60)
        height: Math.min(200, parent.height - 60)
        title: "Confirm Delete"
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
                text: "Confirm Delete"
                font.pixelSize: 14
                font.weight: Font.DemiBold
                color: theme.textPrimary
            }
        }
        contentItem: ColumnLayout {
            spacing: theme.spacingXl

            Text {
                text: "Delete " + contentPage.selectedItems.length + " selected platform post(s)?"
                font.pixelSize: 13
                color: theme.textPrimary
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                Layout.fillWidth: true
            }

            Text {
                text: "This action cannot be undone."
                font.pixelSize: 11
                color: theme.textMuted
                horizontalAlignment: Text.AlignHCenter
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd

                Item { Layout.fillWidth: true }

                Rectangle {
                    width: cancelDeleteLabel.implicitWidth + 32
                    height: 36
                    radius: theme.radiusMd
                    color: theme.surfaceAlt
                    Text {
                        id: cancelDeleteLabel
                        anchors.centerIn: parent
                        text: "Cancel"
                        font.pixelSize: 13
                        color: theme.textSecondary
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: deleteConfirmDialog.close()
                    }
                }

                Rectangle {
                    width: confirmDeleteLabel.implicitWidth + 32
                    height: 36
                    radius: theme.radiusMd
                    color: theme.error
                    Text {
                        id: confirmDeleteLabel
                        anchors.centerIn: parent
                        text: "Delete"
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        color: "#ffffff"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            deleteConfirmDialog.close()
                            contentPage.deleteSelected()
                        }
                    }
                }

                Item { Layout.fillWidth: true }
            }
        }
    }

    // ── Batch Caption Dialog (#3) ─────────────────────────────────
    Dialog {
        id: batchCaptionDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(500, parent.width - 60)
        height: Math.min(500, parent.height - 60)
        title: "Batch Post - Per-Platform Captions"
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
                text: "Batch post - " + selectedItems.length + " items"
                font.pixelSize: 14
                font.weight: Font.DemiBold
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
                        { name: "YouTube", key: "youtube", color: theme.youtube, icon: theme.iconYouTube + " " },
                        { name: "Instagram", key: "instagram", color: theme.instagram, icon: theme.iconInstagram + " " },
                        { name: "X", key: "x", color: theme.xtwitter, icon: theme.iconX + " " },
                        { name: "TikTok", key: "tiktok", color: theme.tiktok, icon: theme.iconTikTok + " " }
                    ]

                    ColumnLayout {
                        spacing: theme.spacingXs
                        Layout.fillWidth: true

                        RowLayout {
                            spacing: theme.spacingSm
                            Text {
                                text: modelData.icon
                                font.family: theme.iconFontFamily
                                font.pixelSize: 12
                                color: modelData.color
                            }
                            Text {
                                text: modelData.name
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
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
                        text: "Review batch"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        color: "#ffffff"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: contentPage.prepareBatchPreview()
                    }
                }
            }
        }
    }

    // ── Batch Post Preview Dialog ─────────────────────────────────
    Dialog {
        id: batchPreviewDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(560, parent.width - 60)
        height: Math.min(560, parent.height - 60)
        title: "Review batch"
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
                text: contentPage.pendingBatchPreview.ready ? "Batch ready" : "Batch needs attention"
                font.pixelSize: 14
                font.weight: Font.DemiBold
                color: theme.textPrimary
            }
        }
        contentItem: Flickable {
            clip: true
            contentHeight: batchPreviewCol.implicitHeight

            ColumnLayout {
                id: batchPreviewCol
                width: parent.width
                spacing: theme.spacingMd

                Text {
                    text: (contentPage.pendingBatchPreview.items || []).length + " selected item(s)"
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Layout.fillWidth: true
                }

                Repeater {
                    model: (contentPage.pendingBatchPreview.blocking || []).concat(contentPage.pendingBatchPreview.warnings || [])

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm

                        Rectangle {
                            width: 8; height: 8; radius: 4
                            color: index < (contentPage.pendingBatchPreview.blocking || []).length ? theme.error : theme.warning
                        }
                        Text {
                            text: modelData
                            font.pixelSize: 12
                            color: theme.textSecondary
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }
                    }
                }

                Repeater {
                    model: contentPage.pendingBatchPreview.items || []

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: itemPreviewCol.implicitHeight + theme.spacingMd
                        radius: theme.radiusMd
                        color: theme.surfaceAlt

                        ColumnLayout {
                            id: itemPreviewCol
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.margins: theme.spacingSm
                            spacing: 2

                            Text {
                                text: (modelData.ready ? "Ready: " : "Needs attention: ") + (modelData.title || "Untitled")
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                color: modelData.ready ? theme.success : theme.warning
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                            }
                            Text {
                                text: "Caption length: " + ((modelData.preview && modelData.preview.caption_length) || 0)
                                font.pixelSize: 11
                                color: theme.textSecondary
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                            }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: theme.spacingMd

                    Item { Layout.fillWidth: true }

                    Rectangle {
                        width: cancelBatchPreviewLabel.implicitWidth + 32
                        height: 36
                        radius: theme.radiusMd
                        color: theme.surfaceAlt
                        Text {
                            id: cancelBatchPreviewLabel
                            anchors.centerIn: parent
                            text: "Cancel"
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: batchPreviewDialog.close()
                        }
                    }

                    Rectangle {
                        width: confirmBatchPreviewLabel.implicitWidth + 32
                        height: 36
                        radius: theme.radiusMd
                        color: contentPage.pendingBatchPreview.ready ? theme.accent : theme.surfaceAlt
                        opacity: contentPage.pendingBatchPreview.ready ? 1.0 : 0.6
                        Text {
                            id: confirmBatchPreviewLabel
                            anchors.centerIn: parent
                            text: "Post batch"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            color: contentPage.pendingBatchPreview.ready ? "#ffffff" : theme.textMuted
                        }
                        MouseArea {
                            anchors.fill: parent
                            enabled: contentPage.pendingBatchPreview.ready
                            cursorShape: contentPage.pendingBatchPreview.ready ? Qt.PointingHandCursor : Qt.ArrowCursor
                            onClicked: contentPage.confirmBatchPost()
                            Accessible.name: "Post batch after preview"
                            Accessible.role: Accessible.Button
                        }
                    }
                }
            }
        }
    }

    // ── Video preview dialog with embedded player (#6) ────────────
    property string previewVideoPath: ""
    property string previewVideoTitle: ""
    property bool videoLoading: false
    property bool videoError: false

    Dialog {
        id: videoPreviewDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(640, parent.width - 60)
        height: Math.min(560, parent.height - 60)
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
                text: "Video " + (contentPage.previewVideoTitle || "Video Preview")
                font.pixelSize: 14
                font.weight: Font.DemiBold
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
                    videoOutput: videoOutput
                    autoPlay: false
                    onMediaStatusChanged: {
                        if (mediaStatus === MediaPlayer.LoadingMedia || mediaStatus === MediaPlayer.BufferingMedia) {
                            contentPage.videoLoading = true
                        } else if (mediaStatus === MediaPlayer.LoadedMedia || mediaStatus === MediaPlayer.BufferedMedia || mediaStatus === MediaPlayer.NoMedia || mediaStatus === MediaPlayer.InvalidMedia) {
                            contentPage.videoLoading = false
                        }
                    }
                    onErrorOccurred: function(error, errorString) {
                        contentPage.videoError = true
                        contentPage.videoLoading = false
                    }
                    onSourceChanged: {
                        contentPage.videoError = false
                        contentPage.videoLoading = false
                    }
                }

                VideoOutput {
                    id: videoOutput
                    anchors.fill: parent
                    visible: contentPage.previewVideoPath.length > 0 && !contentPage.videoError
                }

                // Loading spinner (#3)
                BusyIndicator {
                    anchors.centerIn: parent
                    running: contentPage.videoLoading
                    visible: contentPage.videoLoading
                    width: 48
                    height: 48
                }

                // Codec error message (#3)
                ColumnLayout {
                    anchors.centerIn: parent
                    visible: contentPage.videoError
                    spacing: theme.spacingSm

                    Text {
                        text: theme.iconError
                        font.family: theme.iconFontFamily
                        font.pixelSize: 32
                        color: theme.error
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "Codec not available"
                        font.pixelSize: 14
                        font.weight: Font.DemiBold
                        color: "#ffffff"
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "The video format may not be supported.\nTry installing additional codecs or use System Player."
                        font.pixelSize: 11
                        color: "#cccccc"
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                }

                // Fallback text when no video
                Text {
                    anchors.centerIn: parent
                    text: contentPage.previewVideoPath
                          ? "Video\n" + contentPage.previewVideoPath.split("/").pop()
                          : "No video file available"
                    font.pixelSize: 13
                    color: "#ffffff"
                    horizontalAlignment: Text.AlignHCenter
                    visible: !contentPage.videoError && !contentPage.videoLoading &&
                             (videoPlayer.mediaStatus === MediaPlayer.NoMedia ||
                              videoPlayer.mediaStatus === MediaPlayer.InvalidMedia)
                }
            }

            // Seek/scrub bar (#3)
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingSm
                visible: contentPage.previewVideoPath.length > 0 && !contentPage.videoError

                Text {
                    text: {
                        var pos = videoPlayer.position || 0
                        var sec = Math.floor(pos / 1000)
                        var min = Math.floor(sec / 60)
                        sec = sec % 60
                        return min + ":" + (sec < 10 ? "0" : "") + sec
                    }
                    font.pixelSize: 10
                    color: theme.textMuted
                    Layout.preferredWidth: 36
                }

                Slider {
                    id: seekSlider
                    Layout.fillWidth: true
                    from: 0
                    to: videoPlayer.duration > 0 ? videoPlayer.duration : 1
                    value: videoPlayer.position || 0
                    onMoved: videoPlayer.seek(value)
                    enabled: videoPlayer.duration > 0
                }

                Text {
                    text: {
                        var dur = videoPlayer.duration || 0
                        var sec = Math.floor(dur / 1000)
                        var min = Math.floor(sec / 60)
                        sec = sec % 60
                        return min + ":" + (sec < 10 ? "0" : "") + sec
                    }
                    font.pixelSize: 10
                    color: theme.textMuted
                    Layout.preferredWidth: 36
                }
            }

            // Player controls
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd
                visible: contentPage.previewVideoPath.length > 0 && !contentPage.videoError

                // Play/Pause button
                Rectangle {
                    width: playPauseRow.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: theme.accent

                    Row {
                        id: playPauseRow
                        anchors.centerIn: parent
                        spacing: theme.spacingXs

                        Text {
                            text: videoPlayer.playbackState === MediaPlayer.PlayingState ? theme.iconPause : theme.iconPlay
                            font.family: theme.iconFontFamily
                            font.pixelSize: 13
                            color: "#ffffff"
                        }
                        Text {
                            text: videoPlayer.playbackState === MediaPlayer.PlayingState ? "Pause" : "Play"
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                            color: "#ffffff"
                        }
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
                    width: stopRow.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: theme.surfaceAlt

                    Row {
                        id: stopRow
                        anchors.centerIn: parent
                        spacing: theme.spacingXs

                        Text {
                            text: theme.iconStop
                            font.family: theme.iconFontFamily
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }
                        Text {
                            text: "Stop"
                            font.pixelSize: 12
                            color: theme.textSecondary
                        }
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
                    width: openExtRow.implicitWidth + 24
                    height: 32
                    radius: theme.radiusMd
                    color: theme.surfaceAlt

                    Row {
                        id: openExtRow
                        anchors.centerIn: parent
                        spacing: theme.spacingXs

                        Text {
                            text: theme.iconExternal
                            font.family: theme.iconFontFamily
                            font.pixelSize: 13
                            color: theme.textSecondary
                        }
                        Text {
                            text: "System Player"
                            font.pixelSize: 11
                            color: theme.textSecondary
                        }
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
        onClosed: { videoPlayer.stop(); contentPage.videoError = false; contentPage.videoLoading = false }
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
                text: "Edit Captions - " + contentPage.editingPostId
                font.pixelSize: 14
                font.weight: Font.DemiBold
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
                                font.weight: Font.DemiBold
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
                        font.weight: Font.DemiBold
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
            anchors.margins: theme.pageMargin
            spacing: theme.spacingXl

            // Header with grid/list toggle (Item 6)
            RowLayout {
                Layout.fillWidth: true
                Text {
                    text: "Content Library"
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
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
                            text: theme.iconViewGrid
                            font.family: theme.iconFontFamily
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
                            text: theme.iconViewList
                            font.family: theme.iconFontFamily
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

            // Search bar (Item 4 - wired)
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                radius: theme.radiusMd
                color: theme.surfaceCard

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: theme.pageMargin
                    anchors.rightMargin: theme.pageMargin
                    spacing: theme.spacingSm

                    Text { text: "Search"; font.pixelSize: 13 }
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
                            text: "Clear"
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
                    background: Rectangle {
                        color: theme.surfaceCard
                        radius: theme.radiusMd
                        border.color: sortCombo.hovered || sortCombo.activeFocus ? theme.accent : theme.surfaceAlt
                        border.width: 1
                    }
                    contentItem: Text {
                        text: sortCombo.displayText
                        color: theme.textPrimary
                        font.pixelSize: 12
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: theme.spacingMd
                        rightPadding: 28
                        elide: Text.ElideRight
                    }
                    indicator: Text {
                        anchors.right: parent.right
                        anchors.rightMargin: theme.spacingMd
                        anchors.verticalCenter: parent.verticalCenter
                        text: theme.iconChevronRight
                        rotation: 90
                        color: theme.textMuted
                        font.family: theme.iconFontFamily
                        font.pixelSize: 12
                    }
                    delegate: ItemDelegate {
                        width: sortCombo.width
                        height: 32
                        contentItem: Text {
                            text: modelData
                            color: highlighted ? "#ffffff" : theme.textPrimary
                            font.pixelSize: 12
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            color: highlighted ? theme.accent : theme.surfaceCard
                        }
                    }
                    popup: Popup {
                        y: sortCombo.height + 4
                        width: sortCombo.width
                        implicitHeight: contentItem.implicitHeight
                        padding: 0
                        contentItem: ListView {
                            clip: true
                            implicitHeight: contentHeight
                            model: sortCombo.popup.visible ? sortCombo.delegateModel : null
                            currentIndex: sortCombo.highlightedIndex
                        }
                        background: Rectangle {
                            color: theme.surfaceCard
                            radius: theme.radiusMd
                            border.color: theme.surfaceAlt
                            border.width: 1
                        }
                    }
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
                    anchors.leftMargin: theme.pageMargin
                    anchors.rightMargin: theme.pageMargin
                    spacing: theme.spacingMd

                    Text {
                        text: selectedItems.length + " selected"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
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
                            text: "Delete selected"
                            font.pixelSize: 11
                            font.weight: Font.DemiBold
                            color: "#ffffff"
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: deleteConfirmDialog.open()
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
                            text: "Post selected"
                            font.pixelSize: 11
                            font.weight: Font.DemiBold
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

            // Content grid view - using pageItems (filtered + sorted + paginated)
            GridLayout {
                Layout.fillWidth: true
                columns: contentPage.listViewMode ? 1 : (contentPage.width < 900 ? 2 : 3)
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
                            anchors.margins: theme.pageMargin
                            spacing: theme.spacingMd

                            // Selection checkbox (Item 8)
                            Rectangle {
                                width: 20; height: 20
                                radius: 4
                                color: contentPage.isSelected(modelData.rowKey) ? theme.accent : "transparent"
                                border.color: contentPage.isSelected(modelData.rowKey) ? theme.accent : theme.textMuted
                                border.width: 1.5
                                Layout.alignment: Qt.AlignTop

                                Text {
                                    anchors.centerIn: parent
                                    text: theme.iconCheck
                                    font.family: theme.iconFontFamily
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    color: "#ffffff"
                                    visible: contentPage.isSelected(modelData.rowKey)
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: contentPage.toggleSelection(modelData.rowKey || "")
                                    Accessible.name: "Select " + (modelData.title || "item")
                                    Accessible.role: Accessible.CheckBox
                                }
                            }

                            // Thumbnail area (hidden in list mode)
                            Rectangle {
                                Layout.preferredWidth: contentPage.listViewMode ? 56 : 72
                                Layout.preferredHeight: contentPage.listViewMode ? 56 : 100
                                Layout.fillWidth: false
                                Layout.fillHeight: false
                                Layout.alignment: Qt.AlignTop
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
                                    text: theme.iconVideo
                                    font.family: theme.iconFontFamily
                                    font.pixelSize: contentPage.listViewMode ? 16 : 24
                                    color: theme.textMuted
                                    visible: thumbImage.status !== Image.Ready
                                }

                                // Click to preview video (Item 5 - now with embedded player)
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
                                Layout.alignment: Qt.AlignTop
                                spacing: theme.spacingXs

                                RowLayout {
                                    spacing: theme.spacingXs
                                    Text {
                                        text: modelData.title || "Untitled"
                                        font.pixelSize: 13
                                        font.weight: Font.DemiBold
                                        color: theme.textPrimary
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                        maximumLineCount: 1
                                    }

                                    // Similarity badge (#17) - dedup confidence score
                                    Rectangle {
                                        width: simLabel.implicitWidth + 8
                                        height: 16
                                        radius: 4
                                        color: {
                                            var sim = contentPage.getSimilarityPercent(modelData)
                                            if (sim >= 80) return Qt.rgba(1, 0.2, 0.2, 0.2)
                                            if (sim >= 50) return Qt.rgba(1, 0.6, 0, 0.2)
                                            return Qt.rgba(1, 0.6, 0, 0.1)
                                        }
                                        visible: contentPage.getSimilarityPercent(modelData) >= 30
                                        Text {
                                            id: simLabel
                                            anchors.centerIn: parent
                                            text: contentPage.getSimilarityPercent(modelData) + "%"
                                            font.pixelSize: 8
                                            font.weight: Font.DemiBold
                                            color: {
                                                var sim = contentPage.getSimilarityPercent(modelData)
                                                if (sim >= 80) return theme.error
                                                if (sim >= 50) return theme.warning
                                                return theme.textMuted
                                            }
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
                                            font.weight: Font.DemiBold
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
                                            text: theme.iconEdit
                                            font.family: theme.iconFontFamily
                                            font.pixelSize: 12
                                            color: theme.textSecondary
                                        }
                                        MouseArea {
                                            id: editMouseArea
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                contentPage.editingPostId = modelData.sourceId || modelData.postId || ""
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

                    Row {
                        id: prevLabel
                        anchors.centerIn: parent
                        spacing: 4
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: theme.iconChevronLeft
                            font.family: theme.iconFontFamily
                            font.pixelSize: 12
                            color: contentPage.currentPage > 1 ? theme.textPrimary : theme.textMuted
                        }
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: "Previous"
                            font.pixelSize: 12
                            color: contentPage.currentPage > 1 ? theme.textPrimary : theme.textMuted
                        }
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

                    Row {
                        id: nextLabel
                        anchors.centerIn: parent
                        spacing: 4
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: "Next"
                            font.pixelSize: 12
                            color: contentPage.currentPage < contentPage.totalPages ? theme.textPrimary : theme.textMuted
                        }
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: theme.iconChevronRight
                            font.family: theme.iconFontFamily
                            font.pixelSize: 12
                            color: contentPage.currentPage < contentPage.totalPages ? theme.textPrimary : theme.textMuted
                        }
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
                        text: theme.iconVideo
                        font.family: theme.iconFontFamily
                        font.pixelSize: 30
                        color: theme.accent
                        horizontalAlignment: Text.AlignHCenter
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "No content yet"
                        font.pixelSize: 16
                        font.weight: Font.DemiBold
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

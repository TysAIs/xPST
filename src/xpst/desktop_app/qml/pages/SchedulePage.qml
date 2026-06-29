import QtQuick 2.15
import xpst.desktop_app.qml 1.0
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Page {
    id: schedulePage
    background: Rectangle { color: theme.canvas }

    property date currentDate: new Date()
    property int displayedMonth: currentDate.getMonth()
    property int displayedYear: currentDate.getFullYear()
    property int currentMonth: displayedMonth
    property int currentYear: displayedYear
    property var scheduledPosts: []

    // Form state for the "Create Schedule" card
    property string formVideoPath: ""
    property string formCaption: ""
    property var formPlatforms: ({ youtube: true, instagram: false, x: false, tiktok: false, threads: false, linkedin: false })
    property string formDatetime: ""        // 'YYYY-MM-DD HH:MM'
    property int formRecurrence: 0          // 0=One-time, 1=Daily, 2=Weekly
    property string formError: ""

    function reloadScheduledPosts() {
        if (typeof controller === "undefined" || !controller.listScheduledPosts) return
        try {
            var raw = controller.listScheduledPosts()
            var parsed = JSON.parse(raw)
            scheduledPosts = parsed.ok ? (parsed.posts || []) : []
        } catch(e) {
            console.error("SchedulePage: failed to load scheduled posts", e)
            scheduledPosts = []
        }
    }

    Component.onCompleted: reloadScheduledPosts()

    // Helper: format a schedule entry's scheduled_time for display
    function formatScheduleTime(ts) {
        if (!ts) return ""
        try {
            return new Date(ts).toLocaleString()
        } catch(e) {
            console.error("SchedulePage: bad timestamp", ts, e)
            return ts
        }
    }

    // Submit the create-schedule form: validate, call backend, refresh list.
    function submitScheduleForm() {
        formError = ""
        if (!formVideoPath) {
            formError = "Please choose a video file"
            return
        }
        if (!formCaption) {
            formError = "Please enter a caption"
            return
        }
        if (!formDatetime) {
            formError = "Please pick a date and time (YYYY-MM-DD HH:MM)"
            return
        }
        var platforms = []
        if (formPlatforms.youtube) platforms.push("youtube")
        if (formPlatforms.instagram) platforms.push("instagram")
        if (formPlatforms.x) platforms.push("x")
        if (formPlatforms.tiktok) platforms.push("tiktok")
        if (formPlatforms.threads) platforms.push("threads")
        if (formPlatforms.linkedin) platforms.push("linkedin")
        if (platforms.length === 0) {
            formError = "Select at least one platform"
            return
        }
        var recurrenceNames = ["one_time", "daily", "weekly"]
        var payload = {
            video_path: formVideoPath,
            caption: formCaption,
            scheduled_time: formDatetime,
            platforms: platforms,
            repeat_rule: recurrenceNames[formRecurrence] || "one_time"
        }
        try {
            // Persist schedule-related defaults into the config as well
            if (typeof controller !== "undefined" && controller.saveSettings) {
                controller.saveSettings(JSON.stringify({ schedule: { last_platforms: platforms.join(",") } }))
            }
            var raw = controller.addScheduledPost(JSON.stringify(payload))
            var parsed = JSON.parse(raw)
            if (!parsed.ok) {
                formError = parsed.error || "Failed to schedule post"
                return
            }
            // Reset form
            formVideoPath = ""
            formCaption = ""
            formDatetime = ""
            formRecurrence = 0
            formPlatforms = ({ youtube: true, instagram: false, x: false, tiktok: false })
            reloadScheduledPosts()
            showToast("Post scheduled for " + (parsed.post ? parsed.post.scheduled_time : ""), false)
        } catch(e) {
            console.error("SchedulePage: addScheduledPost failed", e)
            formError = "Scheduling failed: " + e
        }
    }

    // Delete a scheduled post by id, then refresh the list.
    function deleteScheduledPost(entryId) {
        if (!entryId) return
        try {
            var raw = controller.removeScheduledPost(entryId)
            var parsed = JSON.parse(raw)
            if (parsed.ok) {
                showToast("Scheduled post removed", false)
            } else {
                showToast("Could not remove post: " + (parsed.error || ""), true)
            }
            reloadScheduledPosts()
        } catch(e) {
            console.error("SchedulePage: removeScheduledPost failed", e)
            showToast("Failed to remove scheduled post", true)
        }
    }

    function closeDialog() { if (dayPostsPopup.visible) dayPostsPopup.close() }

    // Build calendar data
    property int firstDayOfMonth: new Date(displayedYear, displayedMonth, 1).getDay()
    property int daysInMonth: new Date(displayedYear, displayedMonth + 1, 0).getDate()
    property string monthName: {
        var names = ["January", "February", "March", "April", "May", "June",
                     "July", "August", "September", "October", "November", "December"]
        return names[displayedMonth] + " " + displayedYear
    }

    // Days that have scheduled posts (from ScheduleManager entries)
    property var scheduledDays: {
        var days = {}
        try {
            var posts = schedulePage.scheduledPosts
            for (var i = 0; i < posts.length; i++) {
                var ts = posts[i].scheduled_time || ""
                if (ts) {
                    var d = new Date(ts)
                    if (d.getMonth() === displayedMonth && d.getFullYear() === displayedYear) {
                        days[d.getDate()] = true
                    }
                }
            }
        } catch(e) { console.error("SchedulePage: failed to build scheduled-days map", e) }
        return days
    }

    // Get posts for a specific day (schedule entries)
    function getPostsForDay(dayNum) {
        var result = []
        try {
            var posts = schedulePage.scheduledPosts
            for (var i = 0; i < posts.length; i++) {
                var ts = posts[i].scheduled_time || ""
                if (ts) {
                    var d = new Date(ts)
                    if (d.getDate() === dayNum && d.getMonth() === displayedMonth && d.getFullYear() === displayedYear) {
                        result.push(posts[i])
                    }
                }
            }
        } catch(e) { console.error("SchedulePage: failed to get schedule entries for day", e) }
        return result
    }

    // Selected day for popup
    property int selectedDay: 0
    property var selectedDayPosts: []

    // Day posts popup (#5)
    Dialog {
        id: dayPostsPopup
        modal: true
        anchors.centerIn: parent
        width: Math.min(400, parent.width - 60)
        height: Math.min(360, parent.height - 60)
        title: "Posts on " + schedulePage.monthName + " " + schedulePage.selectedDay
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
                text: "Date " + schedulePage.monthName + " " + schedulePage.selectedDay
                font.pixelSize: 14
                font.weight: Font.DemiBold
                color: theme.textPrimary
            }
        }
        contentItem: ColumnLayout {
            spacing: theme.spacingMd

            Text {
                text: schedulePage.selectedDayPosts.length + " post(s) scheduled"
                font.pixelSize: 12
                color: theme.textSecondary
                visible: schedulePage.selectedDayPosts.length > 0
            }

            Repeater {
                model: schedulePage.selectedDayPosts

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 40
                    radius: theme.radiusSm
                    color: theme.surfaceAlt
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: theme.spacingSm
                        spacing: theme.spacingSm
                        Rectangle {
                            width: 6; height: 6; radius: 3
                            color: theme.accent
                        }
                        Text {
                            text: {
                                var vp = modelData.video_path || ""
                                return vp ? vp.split("/").pop() : (modelData.caption || "Untitled")
                            }
                            font.pixelSize: 12
                            color: theme.textPrimary
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Text {
                            text: ((modelData.platforms || []).join(", ")) || "all"
                            font.pixelSize: 10
                            color: theme.textMuted
                        }
                    }
                }
            }

            Text {
                text: "No posts scheduled for this day"
                font.pixelSize: 12
                color: theme.textMuted
                visible: schedulePage.selectedDayPosts.length === 0
                Layout.alignment: Qt.AlignHCenter
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                radius: theme.radiusMd
                color: theme.accent
                Text {
                    anchors.centerIn: parent
                    text: "+ Schedule New"
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    color: "#ffffff"
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        dayPostsPopup.close()
                        // Pre-fill the datetime with the selected day and focus the form
                        var mm = (schedulePage.displayedMonth + 1)
                        var dd = schedulePage.selectedDay
                        schedulePage.formDatetime = schedulePage.displayedYear + "-" +
                            (mm < 10 ? "0" + mm : mm) + "-" + (dd < 10 ? "0" + dd : dd) + " 09:00"
                        schedFlick.contentY = 0
                        captionField.forceActiveFocus()
                    }
                }
            }
        }
    }

    function prevMonth() {
        if (displayedMonth === 0) {
            displayedMonth = 11
            displayedYear--
        } else {
            displayedMonth--
        }
        currentMonth = displayedMonth
        currentYear = displayedYear
    }

    function nextMonth() {
        if (displayedMonth === 11) {
            displayedMonth = 0
            displayedYear++
        } else {
            displayedMonth++
        }
        currentMonth = displayedMonth
        currentYear = displayedYear
    }

    Flickable {
        id: schedFlick
        anchors.fill: parent
        contentHeight: schedCol.implicitHeight + theme.spacingXxl
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: schedCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: theme.pageMargin
            spacing: theme.spacingXl

            // Header
            ColumnLayout {
                spacing: theme.spacingXs
                Text {
                    text: "Schedule"
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                    Accessible.name: "Schedule page title"
                    Accessible.role: Accessible.Heading
                }
                Text {
                    text: "View your posting schedule"
                    font.pixelSize: 13
                    color: theme.textSecondary
                }
            }

            // ── Create Schedule form ───────────────────────────────
            Rectangle {
                id: createFormCard
                Layout.fillWidth: true
                Layout.preferredHeight: createFormCol.implicitHeight + theme.spacingXxl
                radius: theme.radiusLg
                color: theme.surfaceCard

                ColumnLayout {
                    id: createFormCol
                    anchors.fill: parent
                    anchors.margins: theme.pageMargin
                    spacing: theme.spacingMd

                    Text {
                        text: "Create Schedule"
                        font.pixelSize: 16
                        font.weight: Font.DemiBold
                        color: theme.textPrimary
                    }

                    // Video file picker
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm
                        Text {
                            text: "Video"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.preferredWidth: 72
                        }
                        TextField {
                            id: videoPathField
                            Layout.fillWidth: true
                            placeholderText: "Choose a video file..."
                            text: schedulePage.formVideoPath
                            color: theme.textPrimary
                            placeholderTextColor: theme.textMuted
                            font.pixelSize: 12
                            readOnly: true
                            background: Rectangle {
                                radius: theme.radiusSm
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1
                            }
                            Accessible.name: "Video file path"
                        }
                        Rectangle {
                            width: browseLabel.implicitWidth + 24
                            height: 36
                            radius: theme.radiusMd
                            color: theme.surfaceAlt
                            border.color: theme.textMuted
                            border.width: 1
                            Text {
                                id: browseLabel
                                anchors.centerIn: parent
                                text: "Browse"
                                font.pixelSize: 12
                                color: theme.textPrimary
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (typeof controller !== "undefined" && controller.browseForFolder) {
                                        schedulePage.formVideoPath = controller.browseForFolder()
                                    }
                                }
                            }
                        }
                    }

                    // Caption
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm
                        Text {
                            text: "Caption"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.preferredWidth: 72
                        }
                        TextField {
                            id: captionField
                            Layout.fillWidth: true
                            placeholderText: "Post caption..."
                            text: schedulePage.formCaption
                            color: theme.textPrimary
                            placeholderTextColor: theme.textMuted
                            font.pixelSize: 12
                            onTextChanged: schedulePage.formCaption = text
                            background: Rectangle {
                                radius: theme.radiusSm
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1
                            }
                            Accessible.name: "Caption"
                        }
                    }

                    // Platform checkboxes
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd
                        Text {
                            text: "Platforms"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.preferredWidth: 72
                        }
                        Repeater {
                            model: [
                                { key: "youtube", name: "YouTube", color: theme.youtube },
                                { key: "instagram", name: "Instagram", color: theme.instagram },
                                { key: "x", name: "X", color: theme.xtwitter },
                                { key: "tiktok", name: "TikTok", color: theme.tiktok },
                                { key: "threads", name: "Threads", color: theme.threads },
                                { key: "linkedin", name: "LinkedIn", color: theme.linkedin }
                            ]
                            RowLayout {
                                spacing: theme.spacingXs
                                Rectangle {
                                    width: 18; height: 18; radius: 4
                                    color: schedulePage.formPlatforms[modelData.key] ? theme.accent : "transparent"
                                    border.color: schedulePage.formPlatforms[modelData.key] ? theme.accent : theme.textMuted
                                    border.width: 1.5
                                    Text {
                                        anchors.centerIn: parent
                                        text: Icons.check
                                        font.family: theme.iconFontFamily
                                        font.pixelSize: 12
                                        color: "#ffffff"
                                        visible: schedulePage.formPlatforms[modelData.key]
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: {
                                            var next = {}
                                            for (var k in schedulePage.formPlatforms) next[k] = schedulePage.formPlatforms[k]
                                            next[modelData.key] = !next[modelData.key]
                                            schedulePage.formPlatforms = next
                                        }
                                    }
                                }
                                Text {
                                    text: modelData.name
                                    font.pixelSize: 12
                                    color: theme.textPrimary
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }

                    // Date/time + recurrence
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingSm
                        Text {
                            text: "When"
                            font.pixelSize: 12
                            color: theme.textSecondary
                            Layout.preferredWidth: 72
                        }
                        TextField {
                            id: datetimeField
                            Layout.fillWidth: true
                            placeholderText: "YYYY-MM-DD HH:MM"
                            text: schedulePage.formDatetime
                            color: theme.textPrimary
                            placeholderTextColor: theme.textMuted
                            font.pixelSize: 12
                            onTextChanged: schedulePage.formDatetime = text
                            background: Rectangle {
                                radius: theme.radiusSm
                                color: theme.surfaceAlt
                                border.color: theme.textMuted
                                border.width: 1
                            }
                            Accessible.name: "Scheduled date and time"
                        }
                        ComboBox {
                            id: recurrenceCombo
                            model: ["One-time", "Daily", "Weekly"]
                            currentIndex: schedulePage.formRecurrence
                            onCurrentIndexChanged: schedulePage.formRecurrence = currentIndex
                            Layout.preferredWidth: 130
                            Accessible.name: "Recurrence"
                        }
                    }

                    // Validation error
                    Text {
                        text: schedulePage.formError
                        font.pixelSize: 12
                        color: theme.error
                        visible: schedulePage.formError.length > 0
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                    }

                    // Submit button
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd
                        Rectangle {
                            width: scheduleLabel.implicitWidth + 32
                            height: 38
                            radius: theme.radiusMd
                            color: theme.accent
                            Text {
                                id: scheduleLabel
                                anchors.centerIn: parent
                                text: "Schedule"
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                color: "#ffffff"
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: schedulePage.submitScheduleForm()
                                Accessible.name: "Schedule post"
                                Accessible.role: Accessible.Button
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }

            // Calendar card
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: calendarCol.implicitHeight + theme.spacingXxl
                radius: theme.radiusLg
                color: theme.surfaceCard

                ColumnLayout {
                    id: calendarCol
                    anchors.fill: parent
                    anchors.margins: theme.pageMargin
                    spacing: theme.spacingMd

                    // Month navigation
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.spacingMd

                        Rectangle {
                            width: 32; height: 32
                            radius: theme.radiusSm
                            color: theme.surfaceAlt
                            Text {
                                anchors.centerIn: parent
                                text: Icons.retry
                                font.family: theme.iconFontFamily
                                font.pixelSize: 14
                                color: theme.textPrimary
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: schedulePage.prevMonth()
                            }
                        }

                        Text {
                            text: schedulePage.monthName
                            font.pixelSize: 16
                            font.weight: Font.DemiBold
                            color: theme.textPrimary
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                        }

                        Rectangle {
                            width: 32; height: 32
                            radius: theme.radiusSm
                            color: theme.surfaceAlt
                            Text {
                                anchors.centerIn: parent
                                text: Icons.retry
                                font.family: theme.iconFontFamily
                                font.pixelSize: 14
                                color: theme.textPrimary
                                // Flip horizontally for "next" arrow
                                transform: Scale { xScale: -1; origin.x: width / 2 }
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: schedulePage.nextMonth()
                            }
                        }
                    }

                    // Day-of-week headers
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        Repeater {
                            model: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                            Text {
                                text: modelData
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                                color: theme.textMuted
                                horizontalAlignment: Text.AlignHCenter
                                Layout.fillWidth: true
                            }
                        }
                    }

                    // Calendar grid (7 columns x 6 rows max)
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 7
                        rowSpacing: theme.spacingXs
                        columnSpacing: 0

                        Repeater {
                            model: 42  // 6 rows x 7 days

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 40
                                radius: theme.radiusSm
                                color: {
                                    var dayNum = index - schedulePage.firstDayOfMonth + 1
                                    if (dayNum <= 0 || dayNum > schedulePage.daysInMonth) return "transparent"
                                    var today = new Date()
                                    if (dayNum === today.getDate() && schedulePage.displayedMonth === today.getMonth() && schedulePage.displayedYear === today.getFullYear()) {
                                        return theme.accentMuted
                                    }
                                    return dayMouse.containsMouse ? theme.surfaceAlt : "transparent"
                                }

                                property int dayNum: index - schedulePage.firstDayOfMonth + 1
                                visible: dayNum > 0 && dayNum <= schedulePage.daysInMonth

                                ColumnLayout {
                                    anchors.centerIn: parent
                                    spacing: 2

                                    Text {
                                        text: String(parent.parent.dayNum)
                                        font.pixelSize: 12
                                        color: theme.textPrimary
                                        horizontalAlignment: Text.AlignHCenter
                                        Layout.alignment: Qt.AlignHCenter
                                    }

                                    // Dot for scheduled posts
                                    Rectangle {
                                        width: 6; height: 6; radius: 3
                                        color: theme.accent
                                        visible: schedulePage.scheduledDays[parent.parent.dayNum] === true
                                        Layout.alignment: Qt.AlignHCenter
                                    }
                                }

                                MouseArea {
                                    id: dayMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        if (dayNum > 0 && dayNum <= schedulePage.daysInMonth) {
                                            schedulePage.selectedDay = dayNum
                                            schedulePage.selectedDayPosts = schedulePage.getPostsForDay(dayNum)
                                            dayPostsPopup.open()
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Scheduled posts list
            ColumnLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd

                Text {
                    text: "Scheduled Posts"
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                    color: theme.textPrimary
                }

                Repeater {
                    model: schedulePage.scheduledPosts

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 64
                        radius: theme.radiusMd
                        color: theme.surfaceCard

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingMd
                            spacing: theme.spacingMd

                            Rectangle {
                                width: 8; height: 8; radius: 4
                                color: theme.accent
                            }

                            ColumnLayout {
                                spacing: 2
                                Layout.fillWidth: true
                                Text {
                                    text: {
                                        var vp = modelData.video_path || ""
                                        return vp ? vp.split("/").pop() : (modelData.caption || "Untitled")
                                    }
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    color: theme.textPrimary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: ((modelData.platforms || []).join(", ") || "all platforms") +
                                          " / " + (modelData.status || "pending") +
                                          (modelData.repeat_rule ? " / " + modelData.repeat_rule : "")
                                    font.pixelSize: 11
                                    color: theme.textMuted
                                }
                            }

                            Text {
                                text: schedulePage.formatScheduleTime(modelData.scheduled_time || "")
                                font.pixelSize: 11
                                color: theme.textMuted
                            }

                            // Delete button
                            Rectangle {
                                width: deleteSchedLabel.implicitWidth + 24
                                height: 30
                                radius: theme.radiusSm
                                color: deleteSchedMouse.containsMouse ? theme.error : theme.surfaceAlt
                                Text {
                                    id: deleteSchedLabel
                                    anchors.centerIn: parent
                                    text: "Delete"
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                    color: deleteSchedMouse.containsMouse ? "#ffffff" : theme.textSecondary
                                }
                                MouseArea {
                                    id: deleteSchedMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: schedulePage.deleteScheduledPost(modelData.id || "")
                                    Accessible.name: "Delete scheduled post"
                                    Accessible.role: Accessible.Button
                                }
                            }
                        }
                    }
                }

                // Empty state
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 80
                    color: "transparent"
                    visible: schedulePage.scheduledPosts.length === 0

                    ColumnLayout {
                        anchors.centerIn: parent
                        spacing: theme.spacingSm
                        Text {
                            text: Icons.calendar
                            font.family: theme.iconFontFamily
                            font.pixelSize: 24
                            horizontalAlignment: Text.AlignHCenter
                            Layout.alignment: Qt.AlignHCenter
                            color: theme.textMuted
                        }
                        Text {
                            text: "No scheduled posts"
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
}

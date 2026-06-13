import QtQuick 2.15
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

    function closeDialog() { if (dayPostsPopup.visible) dayPostsPopup.close() }

    function scheduledModel() {
        try {
            if (typeof controller !== "undefined")
                return JSON.parse(controller.scheduledPosts || "[]")
        } catch(e) {}
        return []
    }

    function dateForDay(dayNum) {
        var d = new Date(displayedYear, displayedMonth, dayNum)
        var y = d.getFullYear()
        var m = String(d.getMonth() + 1).padStart(2, "0")
        var day = String(d.getDate()).padStart(2, "0")
        return y + "-" + m + "-" + day
    }

    // Build calendar data
    property int firstDayOfMonth: new Date(displayedYear, displayedMonth, 1).getDay()
    property int daysInMonth: new Date(displayedYear, displayedMonth + 1, 0).getDate()
    property string monthName: {
        var names = ["January", "February", "March", "April", "May", "June",
                     "July", "August", "September", "October", "November", "December"]
        return names[displayedMonth] + " " + displayedYear
    }

    // Days that have scheduled posts (from controller data)
    property var scheduledDays: {
        var days = {}
        try {
            var posts = schedulePage.scheduledModel()
            for (var i = 0; i < posts.length; i++) {
                var ts = posts[i].scheduled_time || posts[i].timestamp || ""
                if (ts) {
                    var d = new Date(ts)
                    if (d.getMonth() === displayedMonth && d.getFullYear() === displayedYear) {
                        days[d.getDate()] = true
                    }
                }
            }
        } catch(e) {}
        return days
    }

    // Get posts for a specific day
    function getPostsForDay(dayNum) {
        var result = []
        try {
            var posts = schedulePage.scheduledModel()
            for (var i = 0; i < posts.length; i++) {
                var ts = posts[i].scheduled_time || posts[i].timestamp || ""
                if (ts) {
                    var d = new Date(ts)
                    if (d.getDate() === dayNum && d.getMonth() === displayedMonth && d.getFullYear() === displayedYear) {
                        result.push(posts[i])
                    }
                }
            }
        } catch(e) {}
        return result
    }

    // Selected day for popup
    property int selectedDay: 0
    property var selectedDayPosts: []

    Dialog {
        id: scheduleNewDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(460, parent.width - 48)
        title: "Schedule New"

        function openForDay(dayNum) {
            scheduleDateField.text = schedulePage.dateForDay(dayNum)
            scheduleTimeField.text = "09:00"
            schedulePathField.text = ""
            scheduleCaptionField.text = ""
            youtubeCheck.checked = true
            instagramCheck.checked = true
            xCheck.checked = true
            open()
        }

        function selectedPlatforms() {
            var platforms = []
            if (youtubeCheck.checked) platforms.push("youtube")
            if (instagramCheck.checked) platforms.push("instagram")
            if (xCheck.checked) platforms.push("x")
            return platforms
        }

        function submit() {
            var platforms = selectedPlatforms()
            if (platforms.length === 0) {
                showToast("Select at least one platform", true)
                return
            }
            var whenIso = scheduleDateField.text.trim() + "T" + scheduleTimeField.text.trim() + ":00"
            var ok = controller.scheduleNew(
                schedulePathField.text.trim(),
                scheduleCaptionField.text,
                whenIso,
                JSON.stringify(platforms)
            )
            if (ok) {
                controller.refreshData()
                schedulePage.selectedDayPosts = schedulePage.getPostsForDay(schedulePage.selectedDay)
                close()
            }
        }

        background: Rectangle {
            color: theme.surfaceCard
            radius: theme.radiusLg
        }

        contentItem: ColumnLayout {
            spacing: theme.spacingMd

            Text {
                text: "Video file"
                font.pixelSize: 12
                color: theme.textSecondary
                Layout.fillWidth: true
            }
            TextField {
                id: schedulePathField
                Layout.fillWidth: true
                placeholderText: "Video file path"
                selectByMouse: true
                color: theme.textPrimary
                placeholderTextColor: theme.textMuted
                background: Rectangle {
                    radius: theme.radiusMd
                    color: theme.surfaceAlt
                    border.color: schedulePathField.activeFocus ? theme.accent : theme.textMuted
                    border.width: schedulePathField.activeFocus ? 2 : 1
                }
            }

            Text {
                text: "Caption"
                font.pixelSize: 12
                color: theme.textSecondary
                Layout.fillWidth: true
            }
            TextArea {
                id: scheduleCaptionField
                Layout.fillWidth: true
                Layout.preferredHeight: 96
                placeholderText: "Caption"
                wrapMode: TextEdit.Wrap
                selectByMouse: true
                color: theme.textPrimary
                placeholderTextColor: theme.textMuted
                background: Rectangle {
                    radius: theme.radiusMd
                    color: theme.surfaceAlt
                    border.color: scheduleCaptionField.activeFocus ? theme.accent : theme.textMuted
                    border.width: scheduleCaptionField.activeFocus ? 2 : 1
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingSm
                TextField {
                    id: scheduleDateField
                    Layout.fillWidth: true
                    placeholderText: "YYYY-MM-DD"
                    selectByMouse: true
                    color: theme.textPrimary
                    placeholderTextColor: theme.textMuted
                    background: Rectangle {
                        radius: theme.radiusMd
                        color: theme.surfaceAlt
                        border.color: scheduleDateField.activeFocus ? theme.accent : theme.textMuted
                        border.width: scheduleDateField.activeFocus ? 2 : 1
                    }
                }
                TextField {
                    id: scheduleTimeField
                    Layout.preferredWidth: 110
                    placeholderText: "HH:MM"
                    selectByMouse: true
                    color: theme.textPrimary
                    placeholderTextColor: theme.textMuted
                    background: Rectangle {
                        radius: theme.radiusMd
                        color: theme.surfaceAlt
                        border.color: scheduleTimeField.activeFocus ? theme.accent : theme.textMuted
                        border.width: scheduleTimeField.activeFocus ? 2 : 1
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingLg
                CheckBox {
                    id: youtubeCheck
                    text: "YouTube"
                    indicator: Rectangle {
                        implicitWidth: 18
                        implicitHeight: 18
                        x: 0
                        y: parent.height / 2 - height / 2
                        radius: theme.radiusSm
                        color: parent.checked ? theme.youtube : theme.surfaceAlt
                        border.color: parent.checked ? theme.youtube : theme.textMuted
                        border.width: 1

                        Text {
                            anchors.centerIn: parent
                            text: theme.iconCheck
                            font.family: theme.iconFontFamily
                            font.pixelSize: 12
                            color: "#ffffff"
                            visible: parent.parent.checked
                        }
                    }
                    contentItem: Text {
                        text: parent.text
                        color: theme.textPrimary
                        font.pixelSize: 13
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: parent.indicator.width + parent.spacing
                    }
                }
                CheckBox {
                    id: instagramCheck
                    text: "Instagram"
                    indicator: Rectangle {
                        implicitWidth: 18
                        implicitHeight: 18
                        x: 0
                        y: parent.height / 2 - height / 2
                        radius: theme.radiusSm
                        color: parent.checked ? theme.instagram : theme.surfaceAlt
                        border.color: parent.checked ? theme.instagram : theme.textMuted
                        border.width: 1

                        Text {
                            anchors.centerIn: parent
                            text: theme.iconCheck
                            font.family: theme.iconFontFamily
                            font.pixelSize: 12
                            color: "#ffffff"
                            visible: parent.parent.checked
                        }
                    }
                    contentItem: Text {
                        text: parent.text
                        color: theme.textPrimary
                        font.pixelSize: 13
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: parent.indicator.width + parent.spacing
                    }
                }
                CheckBox {
                    id: xCheck
                    text: "X"
                    indicator: Rectangle {
                        implicitWidth: 18
                        implicitHeight: 18
                        x: 0
                        y: parent.height / 2 - height / 2
                        radius: theme.radiusSm
                        color: parent.checked ? theme.xtwitter : theme.surfaceAlt
                        border.color: parent.checked ? theme.xtwitter : theme.textMuted
                        border.width: 1

                        Text {
                            anchors.centerIn: parent
                            text: theme.iconCheck
                            font.family: theme.iconFontFamily
                            font.pixelSize: 12
                            color: "#ffffff"
                            visible: parent.parent.checked
                        }
                    }
                    contentItem: Text {
                        text: parent.text
                        color: theme.textPrimary
                        font.pixelSize: 13
                        verticalAlignment: Text.AlignVCenter
                        leftPadding: parent.indicator.width + parent.spacing
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingSm
                Item { Layout.fillWidth: true }
                Rectangle {
                    width: scheduleCancelLabel.implicitWidth + 28
                    height: 36
                    radius: theme.radiusMd
                    color: theme.surfaceAlt
                    border.color: theme.textMuted
                    Text {
                        id: scheduleCancelLabel
                        anchors.centerIn: parent
                        text: "Cancel"
                        font.pixelSize: 13
                        color: theme.textSecondary
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: scheduleNewDialog.close()
                        Accessible.name: "Cancel scheduling"
                        Accessible.role: Accessible.Button
                    }
                }
                Rectangle {
                    width: scheduleSubmitLabel.implicitWidth + 28
                    height: 36
                    radius: theme.radiusMd
                    color: theme.accent
                    Text {
                        id: scheduleSubmitLabel
                        anchors.centerIn: parent
                        text: "Schedule"
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        color: "#ffffff"
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: scheduleNewDialog.submit()
                        Accessible.name: "Schedule post"
                        Accessible.role: Accessible.Button
                    }
                }
            }
        }
    }

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
                            color: {
                                var p = (modelData.platform || "").toLowerCase()
                                if (p === "youtube") return theme.youtube
                                if (p === "instagram") return theme.instagram
                                if (p === "x") return theme.xtwitter
                                if (p === "tiktok") return theme.tiktok
                                return theme.accent
                            }
                        }
                        Text {
                            text: modelData.title || modelData.postId || "Untitled"
                            font.pixelSize: 12
                            color: theme.textPrimary
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        Text {
                            text: modelData.platform || ""
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
                RowLayout {
                    anchors.centerIn: parent
                    spacing: theme.spacingXs
                    Text {
                        text: theme.iconPlus
                        font.family: theme.iconFontFamily
                        font.pixelSize: 12
                        color: "#ffffff"
                    }
                    Text {
                        text: "Schedule New"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        color: "#ffffff"
                    }
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        dayPostsPopup.close()
                        scheduleNewDialog.openForDay(schedulePage.selectedDay)
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
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.spacingMd

                ColumnLayout {
                    spacing: theme.spacingXs
                    Layout.fillWidth: true
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

                Rectangle {
                    Layout.preferredWidth: scheduleNewHeaderLabel.implicitWidth + 48
                    Layout.preferredHeight: 40
                    radius: theme.radiusMd
                    color: headerScheduleMouse.containsMouse ? theme.accentHover : theme.accent
                    RowLayout {
                        anchors.centerIn: parent
                        spacing: theme.spacingXs
                        Text {
                            text: theme.iconPlus
                            font.family: theme.iconFontFamily
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                            color: "#ffffff"
                        }
                        Text {
                            id: scheduleNewHeaderLabel
                            text: "Schedule New"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            color: "#ffffff"
                        }
                    }
                    MouseArea {
                        id: headerScheduleMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            var today = new Date()
                            schedulePage.selectedDay = today.getDate()
                            scheduleNewDialog.openForDay(schedulePage.selectedDay)
                        }
                        Accessible.name: "Schedule new post"
                        Accessible.role: Accessible.Button
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
                                text: theme.iconChevronLeft
                                font.family: theme.iconFontFamily
                                font.pixelSize: 15
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
                                text: theme.iconChevronRight
                                font.family: theme.iconFontFamily
                                font.pixelSize: 15
                                color: theme.textPrimary
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
                    model: {
                        try {
                            var posts = schedulePage.scheduledModel()
                            var filtered = []
                            for (var i = 0; i < Math.min(posts.length, 10); i++) {
                                filtered.push(posts[i])
                            }
                            return filtered
                        } catch(e) {}
                        return []
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 56
                        radius: theme.radiusMd
                        color: theme.surfaceCard

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: theme.spacingMd
                            spacing: theme.spacingMd

                            Rectangle {
                                width: 8; height: 8; radius: 4
                                color: {
                                    var p = (modelData.platform || "").toLowerCase()
                                    if (p === "youtube") return theme.youtube
                                    if (p === "instagram") return theme.instagram
                                    if (p === "x") return theme.xtwitter
                                    if (p === "tiktok") return theme.tiktok
                                    return theme.accent
                                }
                            }

                            ColumnLayout {
                                spacing: 2
                                Layout.fillWidth: true
                                Text {
                                    text: modelData.title || modelData.postId || "Untitled"
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    color: theme.textPrimary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: (modelData.platform || "") + " / " + (modelData.status || "")
                                    font.pixelSize: 11
                                    color: theme.textMuted
                                }
                            }

                            Text {
                                text: {
                                    var ts = modelData.timestamp || ""
                                    if (!ts) return ""
                                    try {
                                        var d = new Date(ts)
                                        return d.toLocaleDateString()
                                    } catch(e) { return ts }
                                }
                                font.pixelSize: 11
                                color: theme.textMuted
                            }
                        }
                    }
                }

                // Empty state
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 80
                    color: "transparent"
                    visible: {
                        try {
                            var posts = schedulePage.scheduledModel()
                            return posts.length === 0
                        } catch(e) { return true }
                    }

                    ColumnLayout {
                        anchors.centerIn: parent
                        spacing: theme.spacingSm
                        Text {
                            text: theme.iconCalendar
                            font.family: theme.iconFontFamily
                            font.pixelSize: 24
                            color: theme.textMuted
                            horizontalAlignment: Text.AlignHCenter
                            Layout.alignment: Qt.AlignHCenter
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

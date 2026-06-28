// LoadingSkeleton — animated gradient shimmer placeholder (Phase 3.1).
//
// Replaces BusyIndicator for content-loading states with a skeleton block
// that sweeps a subtle highlight left-to-right. Part of the
// xpst.desktop_app.qml module (see qmldir); pages can use
// <LoadingSkeleton/> without an explicit import.
import QtQuick 2.15

Rectangle {
    id: root

    // Default to a neutral surface tone; pages may override.
    color: typeof theme !== "undefined" ? theme.surfaceAlt : "#e5e7eb"
    radius: typeof theme !== "undefined" ? theme.radiusMd : 6
    clip: true

    // When false the shimmer stops (skeleton can remain visible as a static
    // block or be hidden by the caller via `visible`).
    property bool running: true

    // Subtle highlight band. White at low alpha reads well on both light and
    // dark surface tones.
    Rectangle {
        id: shimmer
        width: root.width * 0.45
        height: root.height
        x: -width
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: Qt.rgba(1, 1, 1, 0.0) }
            GradientStop { position: 0.5; color: Qt.rgba(1, 1, 1, 0.22) }
            GradientStop { position: 1.0; color: Qt.rgba(1, 1, 1, 0.0) }
        }
    }

    SequentialAnimation {
        running: root.running && root.visible
        loops: Animation.Infinite
        NumberAnimation {
            target: shimmer
            property: "x"
            from: -shimmer.width
            to: root.width
            duration: 1200
            easing.type: Easing.InOutCubic
        }
    }
}

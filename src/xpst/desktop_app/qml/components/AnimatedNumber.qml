// AnimatedNumber — spring-driven animated numeric counter (Phase 3.1).
//
// Reusable component for analytics counts and metrics: when `value` changes
// the displayed number springs from the old value to the new one instead of
// snapping. Part of the xpst.desktop_app.qml module (see qmldir), so pages
// can use <AnimatedNumber/> without an explicit import.
import QtQuick 2.15

Item {
    id: root

    // The target value to display. Changing this triggers the spring.
    property real value: 0
    // Number of decimal places to render.
    property int decimals: 0
    // Text styling passthrough.
    property color color: theme ? theme.textPrimary : "#000000"
    property int pixelSize: 20
    property bool demiBold: true
    property string fontFamily: ""
    // Spring tuning (Phase 3 spec: spring 1.5, damping 0.4, mass 0.5).
    property real spring: 1.5
    property real damping: 0.4
    property real mass: 0.5

    // Internal displayed value, animated toward `value`.
    property real displayValue: value
    Behavior on displayValue {
        SpringAnimation {
            spring: root.spring
            damping: root.damping
            mass: root.mass
        }
    }

    implicitWidth: label.implicitWidth
    implicitHeight: label.implicitHeight

    Text {
        id: label
        anchors.fill: parent
        text: {
            var p = Math.pow(10, root.decimals)
            var v = Math.round(root.displayValue * p) / p
            // Fixed-precision locale-formatted string (thousands separators).
            return v.toLocaleString(Qt.locale(), "f", root.decimals)
        }
        font.pixelSize: root.pixelSize
        font.weight: root.demiBold ? Font.DemiBold : Font.Normal
        color: root.color
        font.family: root.fontFamily
    }
}

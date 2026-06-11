# PySide6 / QML Design Patterns for Modern SaaS Desktop Apps

> Research compiled for XPST — a modern desktop app built with PySide6 + QML

---

## Table of Contents

1. [Architecture & Technology Choices](#1-architecture--technology-choices)
2. [Key GitHub Repos & Resources](#2-key-github-repos--resources)
3. [Design Inspiration: Linear, Raycast, Modern SaaS](#3-design-inspiration-linear-raycast-modern-saas)
4. [Dark Theme Implementation](#4-dark-theme-implementation)
5. [Custom Frameless Window & Title Bar](#5-custom-frameless-window--title-bar)
6. [Sidebar Navigation Pattern](#6-sidebar-navigation-pattern)
7. [Page Navigation (StackView)](#7-page-navigation-stackview)
8. [Card Component](#8-card-component)
9. [Dashboard with Charts](#9-dashboard-with-charts)
10. [Settings Page with Toggles](#10-settings-page-with-toggles)
11. [Toast Notifications](#11-toast-notifications)
12. [Custom Scrollbar Styling](#12-custom-scrollbar-styling)
13. [Responsive Layout](#13-responsive-layout)
14. [Glassmorphism Effects](#14-glassmorphism-effects)
15. [Font Loading (Inter)](#15-font-loading-inter)
16. [Material Design 3 in Qt/QML](#16-material-design-3-in-qtqml)
17. [Qt Quick Controls 2 Custom Styling](#17-qt-quick-controls-2-custom-styling)
18. [Python ↔ QML Communication](#18-python--qml-communication)
19. [Recommended Project Structure](#19-recommended-project-structure)
20. [Color Palette Reference](#20-color-palette-reference)

---

## 1. Architecture & Technology Choices

### QML vs Qt Widgets for Modern UI

| Aspect | Qt Widgets (PySide6) | Qt Quick / QML |
|--------|---------------------|----------------|
| **Edit-Run Cycle** | Fastest (no compile) | Medium (needs compile for C++ backend) |
| **Mental Model** | Imperative (how to build) | Declarative (what to build) |
| **Animations** | Hard | Native, easy |
| **Custom Skins** | QSS (limited) | Full pixel control |
| **Best For** | Traditional desktop | Modern SaaS-style UIs |

**Recommendation for XPST:** Use **QML** for the UI layer with **PySide6** Python backend. QML gives us:
- Declarative UI with property bindings (auto-updating)
- Native animation support
- Full visual customization
- Best path to a Linear/Raycast-quality UI

### PySide6 QML App Skeleton

```python
import sys
from pathlib import Path
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

def main():
    app = QGuiApplication(sys.argv)
    app.setOrganizationName("XPST")
    app.setApplicationName("XPST")

    # Set style BEFORE creating engine
    QQuickStyle.setStyle("Material")

    engine = QQmlApplicationEngine()

    # Register Python backend objects here
    # engine.rootContext().setContextProperty("backend", backend)

    qml_file = Path(__file__).parent / "qml" / "Main.qml"
    engine.load(qml_file)

    if not engine.rootObjects():
        sys.exit(-1)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
```

---

## 2. Key GitHub Repos & Resources

### Must-See Repos

| Repository | Stars | What It Offers |
|-----------|-------|----------------|
| [KhamisiKibet/QT-PyQt-PySide-Custom-Widgets](https://github.com/KhamisiKibet/QT-PyQt-PySide-Custom-Widgets) | 981 | Custom widgets library, modern UI components, SCSS styling |
| [SpinnCompany/25-Modern-GUI-Tutorial](https://github.com/SpinnCompany/25-Modern-GUI-Tutorial) | 16 | Complete modern GUI tutorial with project structure |
| [niklashenning/pyqttoast](https://github.com/niklashenning/pyqttoast) | 148 | Toast notifications for PyQt/PySide |
| [ColinDuquesnoy/QDarkStyleSheet](https://github.com/ColinDuquesnoy/QDarkStyleSheet) | 1.2k+ | Dark/light theme framework for Qt |
| [hypengw/QmlMaterial](https://github.com/hypengw/QmlMaterial) | 85 | Material Design 3 implementation for QML |
| [githubuser0xFFFF/qtass-pyside6](https://github.com/githubuser0xFFFF/qtass-pyside6) | — | Qt Advanced Stylesheets — runtime color switching |
| [likianta/lk-qtquick-scaffold](https://github.com/likianta/lk-qtquick-scaffold) | — | Toolset with predefined widgets/themes for QML |
| [hueyyeng/Comel](https://github.com/hueyyeng/Comel) | — | Opinionated PySide6 light/dark theme framework |
| [trin94/PySide6-project-template](https://github.com/trin94/PySide6-project-template) | — | Unofficial PySide6 + QtQuick project template |
| [mastercomdev/QtToastify](https://github.com/mastercomdev/QtToastify) | — | Zero-dependency QML toast library |

### Key Learning Resources

- **LearnQt Guide** — [Three Dashboards, One Weekend](https://www.learnqt.guide/three-dashboards-one-weekend): Builds the same dashboard in Qt Widgets C++, PySide6, and QML. Free 63-page eBook with 6 themes and complete source code.
- **Qt Official Docs** — [Material Style](https://doc.qt.io/qtforpython-6/overviews/qtquickcontrols-material.html)
- **Qt Blog** — [Material 3 Changes in Qt Quick Controls](https://www.qt.io/blog/material-3-changes-in-qt-quick-controls) (Qt 6.5+)
- **DMC Info** — [Using QtCharts in PySide/QML](https://www.dmcinfo.com/blog/17537/using-qtcharts-in-a-pyside-qml-application/)

---

## 3. Design Inspiration: Linear, Raycast, Modern SaaS

### Linear's Design Principles

**Key takeaways from their UI redesign:**
- **LCH color space** for theme generation (perceptually uniform — a red and yellow at lightness 50 look equally light)
- Theme defined by just **3 variables**: base color, accent color, contrast
- **Inter Display** font for headings
- "Inverted L-shape" navigation: sidebar + top header control the main content area
- Leverages **opacities of black/white** for quick iteration and hierarchy
- Unified light and dark themes under one generation system

### Raycast's Design System

**Surface Ladder (4-step dark scale):**
1. **Canvas**: `#07080a` — Page background
2. **Surface**: `#0d0d0d` — Cards/elevated panels
3. **Surface Elevated**: `#101111` — Button-tertiary, text inputs
4. **Surface Card**: `#121212` — App-icon tiles

**Text Hierarchy:**
- **Ink**: `#f4f4f6` — Primary headlines (slightly off-white)
- **Body**: `#cdcdcd` — Default paragraph text
- **Mute**: `#9c9c9d` — Metadata, footer links
- **Ash**: `#6a6b6c` — Disabled text

**Key Design Rules:**
- **No drop shadows** — elevation built from surface-color ladder
- **Base unit**: 8px
- **Radius range**: 4–16px (never flat on cards)
- **Font**: Inter with OpenType features: `"calt", "kern", "liga", "ss03"`
- **Signature gradient**: used sparingly (hero stripe only)

### What to Steal for XPST

1. **Dark surface ladder** — 4 levels of dark gray for depth without shadows
2. **Off-white text** — not pure white, use `#f4f4f6` for headers
3. **Inter font** — clean, modern, excellent readability
4. **8px grid** — consistent spacing system
5. **Minimal chrome** — thin sidebar, no heavy borders
6. **Accent color sparingly** — one vibrant color for CTAs and highlights

---

## 4. Dark Theme Implementation

### Option A: qt-material Library (Qt Widgets)

```bash
pip install qt-material
```

```python
from qt_material import apply_stylesheet

app = QtWidgets.QApplication(sys.argv)
apply_stylesheet(app, theme='dark_teal.xml')

# Custom accent colors
extra = {
    'density_scale': '-1',  # Compact
    'QMenu': {
        'height': 50,
        'padding': '10px 20px 10px 20px'
    }
}
apply_stylesheet(app, theme='dark_teal.xml', extra=extra)
```

Available themes: `dark_amber`, `dark_blue`, `dark_cyan`, `dark_lightgreen`, `dark_pink`, `dark_purple`, `dark_red`, `dark_teal`, `dark_yellow`, and light variants.

### Option B: QDarkStyleSheet (Widgets)

```python
import qdarkstyle
app.setStyleSheet(qdarkstyle.load_stylesheet_pyside6())
```

### Option C: QML Material Style (Recommended for QML)

```qml
// In main.py — set before engine creation
QQuickStyle.setStyle("Material")

// Or via qtquickcontrols2.conf
// [Material]
// Theme=Dark
// Accent=Teal
// Primary=BlueGrey
// Variant=Dense  // For desktop (smaller controls)
```

### Option D: Custom QML Theme Singleton (Best for Full Control)

```qml
// Theme.qml — singleton
pragma Singleton
import QtQuick 2.15

QtObject {
    // Surface ladder (Raycast-inspired)
    readonly property color canvas:      "#0a0a0f"
    readonly property color surface:     "#12121a"
    readonly property color surfaceAlt:  "#1a1a25"
    readonly property color surfaceCard: "#1e1e2a"

    // Text hierarchy
    readonly property color textPrimary:   "#f0f0f5"
    readonly property color textSecondary: "#a0a0b0"
    readonly property color textMuted:     "#6b6b80"
    readonly property color textDisabled:  "#404050"

    // Accent
    readonly property color accent:      "#6366f1"  // Indigo
    readonly property color accentHover: "#818cf8"
    readonly property color accentMuted: "#312e81"

    // Semantic
    readonly property color success: "#22c55e"
    readonly property color warning: "#f59e0b"
    readonly property color error:   "#ef4444"

    // Spacing (8px grid)
    readonly property int spacingXs: 4
    readonly property int spacingSm: 8
    readonly property int spacingMd: 12
    readonly property int spacingLg: 16
    readonly property int spacingXl: 24
    readonly property int spacingXxl: 32

    // Radius
    readonly property int radiusSm: 6
    readonly property int radiusMd: 8
    readonly property int radiusLg: 12
    readonly property int radiusXl: 16
    readonly property int radiusFull: 9999

    // Font sizes
    readonly property int fontXs: 11
    readonly property int fontSm: 12
    readonly property int fontMd: 14
    readonly property int fontLg: 16
    readonly property int fontXl: 20
    readonly property int fontXxl: 28
}
```

Register as singleton in `qmldir`:
```
module XPST
singleton Theme Theme.qml
```

---

## 5. Custom Frameless Window & Title Bar

### Frameless Window (QML)

```qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Window 2.15

ApplicationWindow {
    id: root
    visible: true
    width: 1280
    height: 800
    flags: Qt.FramelessWindowHint | Qt.Window
    color: "transparent"

    // Custom title bar
    Rectangle {
        id: titleBar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 38
        color: Theme.surface

        // Drag to move
        MouseArea {
            anchors.fill: parent
            property point lastMousePos
            onPressed: (mouse) => lastMousePos = mapToGlobal(mouse.x, mouse.y)
            onPositionChanged: (mouse) => {
                var newPos = mapToGlobal(mouse.x, mouse.y)
                var dx = newPos.x - lastMousePos.x
                var dy = newPos.y - lastMousePos.y
                root.x += dx
                root.y += dy
                lastMousePos = newPos
            }
            onDoubleClicked: root.visibility === Window.Maximized
                ? root.showNormal() : root.showMaximized()
        }

        // Window controls (macOS style: close, minimize, maximize)
        Row {
            anchors.left: parent.left
            anchors.leftMargin: 12
            anchors.verticalCenter: parent.verticalCenter
            spacing: 8

            Repeater {
                model: [
                    { color: "#ff5f57", action: () => root.close() },
                    { color: "#febc2e", action: () => root.showMinimized() },
                    { color: "#28c840", action: () => root.visibility === Window.Maximized
                        ? root.showNormal() : root.showMaximized() }
                ]
                Rectangle {
                    width: 12; height: 12; radius: 6
                    color: modelData.color
                    MouseArea {
                        anchors.fill: parent
                        onClicked: modelData.action()
                    }
                }
            }
        }

        // Title text
        Text {
            anchors.centerIn: parent
            text: "XPST"
            color: Theme.textPrimary
            font.pixelSize: 13
            font.weight: Font.Medium
        }
    }
}
```

### Frameless Window (Python — PyQt6/PySide6 Widgets)

```python
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QMainWindow

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint)
        self._drag_pos = QPoint()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
```

### PySideSix-Frameless-Window Package

```bash
pip install PySideSix-Frameless-Window
```

```python
from framelesswindow import FramelessWindow

class MainWindow(FramelessWindow):
    def __init__(self):
        super().__init__()
        self.setTitleBar(YourCustomTitleBar(self))
```

---

## 6. Sidebar Navigation Pattern

### Modern Animated Sidebar (QML)

```qml
// Sidebar.qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: sidebar
    width: collapsed ? 64 : 240
    color: Theme.surface
    Behavior on width { NumberAnimation { duration: 200; easing.type: Easing.InOutQuad } }

    property bool collapsed: false
    property int currentIndex: 0

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 2

        // Logo / App name
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 48

            Text {
                anchors.centerIn: parent
                text: sidebar.collapsed ? "X" : "XPST"
                color: Theme.textPrimary
                font.pixelSize: sidebar.collapsed ? 20 : 18
                font.weight: Font.Bold
            }
        }

        // Navigation items
        Repeater {
            model: [
                { icon: "📊", label: "Dashboard", page: "DashboardPage.qml" },
                { icon: "📝", label: "Posts",      page: "PostsPage.qml" },
                { icon: "📅", label: "Schedule",   page: "SchedulePage.qml" },
                { icon: "📈", label: "Analytics",  page: "AnalyticsPage.qml" },
                { icon: "👥", label: "Audience",   page: "AudiencePage.qml" },
            ]

            delegate: Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 40
                radius: Theme.radiusSm
                color: sidebar.currentIndex === index ? Theme.accentMuted : "transparent"

                Behavior on color { ColorAnimation { duration: 150 } }

                MouseArea {
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: sidebar.currentIndex = index
                    onEntered: parent.color = sidebar.currentIndex === index
                        ? Theme.accentMuted : Theme.surfaceAlt
                    onExited: parent.color = sidebar.currentIndex === index
                        ? Theme.accentMuted : "transparent"
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    spacing: 12

                    Text {
                        text: modelData.icon
                        font.pixelSize: 18
                        Layout.alignment: Qt.AlignVCenter
                    }

                    Text {
                        text: modelData.label
                        color: sidebar.currentIndex === index
                            ? Theme.accent : Theme.textSecondary
                        font.pixelSize: 14
                        font.weight: sidebar.currentIndex === index ? Font.Medium : Font.Normal
                        visible: !sidebar.collapsed
                        Layout.fillWidth: true
                    }
                }
            }
        }

        Item { Layout.fillHeight: true }  // Spacer

        // Collapse toggle
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 36
            radius: Theme.radiusSm
            color: "transparent"

            MouseArea {
                anchors.fill: parent
                onClicked: sidebar.collapsed = !sidebar.collapsed
                hoverEnabled: true
                onEntered: parent.color = Theme.surfaceAlt
                onExited: parent.color = "transparent"
            }

            Text {
                anchors.centerIn: parent
                text: sidebar.collapsed ? "→" : "←"
                color: Theme.textMuted
                font.pixelSize: 16
            }
        }
    }
}
```

### Usage in Main Layout

```qml
RowLayout {
    anchors.fill: parent
    spacing: 0

    Sidebar {
        id: sidebar
        Layout.fillHeight: true
    }

    StackView {
        id: stackView
        Layout.fillWidth: true
        Layout.fillHeight: true
        initialItem: "pages/DashboardPage.qml"
    }
}
```

---

## 7. Page Navigation (StackView)

```qml
// Main.qml
import QtQuick 2.15
import QtQuick.Controls 2.15

ApplicationWindow {
    id: root
    visible: true
    width: 1280; height: 800

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Sidebar {
            onNavigate: (pageUrl) => stackView.replace(pageUrl)
        }

        StackView {
            id: stackView
            Layout.fillWidth: true
            Layout.fillHeight: true
            initialItem: "pages/DashboardPage.qml"

            // Transition animation
            pushEnter: Transition {
                PropertyAnimation { property: "opacity"; from: 0; to: 1; duration: 200 }
                PropertyAnimation { property: "x"; from: 50; to: 0; duration: 200 }
            }
            pushExit: Transition {
                PropertyAnimation { property: "opacity"; from: 1; to: 0; duration: 150 }
            }
            popEnter: Transition {
                PropertyAnimation { property: "opacity"; from: 0; to: 1; duration: 200 }
            }
            popExit: Transition {
                PropertyAnimation { property: "opacity"; from: 1; to: 0; duration: 150 }
                PropertyAnimation { property: "x"; from: 0; to: 50; duration: 150 }
            }
        }
    }
}
```

### Page Template

```qml
// pages/DashboardPage.qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import XPST 1.0

Page {
    background: Rectangle { color: Theme.canvas }

    Flickable {
        anchors.fill: parent
        contentHeight: contentLayout.implicitHeight
        clip: true

        ColumnLayout {
            id: contentLayout
            anchors.fill: parent
            anchors.margins: Theme.spacingXl
            spacing: Theme.spacingLg

            // Page header
            Text {
                text: "Dashboard"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontXxl
                font.weight: Font.Bold
            }

            // Content grid
            GridLayout {
                columns: 3
                Layout.fillWidth: true
                columnSpacing: Theme.spacingLg
                rowSpacing: Theme.spacingLg

                // Stat cards go here
            }
        }
    }
}
```

---

## 8. Card Component

### Stat Card (QML)

```qml
// components/Card.qml
import QtQuick 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: card
    radius: Theme.radiusLg
    color: Theme.surfaceCard
    border.color: Qt.rgba(1, 1, 1, 0.06)
    border.width: 1

    default property alias content: contentLayout.data
    property string title: ""
    property string subtitle: ""

    implicitHeight: contentLayout.implicitHeight + 32

    ColumnLayout {
        id: contentLayout
        anchors.fill: parent
        anchors.margins: Theme.spacingLg
        spacing: Theme.spacingMd

        // Header
        RowLayout {
            Layout.fillWidth: true
            visible: card.title !== ""

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Text {
                    text: card.title
                    color: Theme.textSecondary
                    font.pixelSize: Theme.fontSm
                    font.weight: Font.Medium
                }

                Text {
                    text: card.subtitle
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontXxl
                    font.weight: Font.Bold
                    visible: card.subtitle !== ""
                }
            }
        }
    }

    // Subtle hover effect
    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        onEntered: card.border.color = Qt.rgba(1, 1, 1, 0.12)
        onExited: card.border.color = Qt.rgba(1, 1, 1, 0.06)
        cursorShape: Qt.PointingHandCursor
        // Don't consume clicks — let children handle
        acceptedButtons: Qt.NoButton
    }

    Behavior on border.color { ColorAnimation { duration: 150 } }
}
```

### Stat Card with Metric

```qml
// components/StatCard.qml
import QtQuick 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    radius: Theme.radiusLg
    color: Theme.surfaceCard
    implicitHeight: 120

    property string icon: "📊"
    property string label: "Metric"
    property string value: "0"
    property string change: "+0%"
    property bool changePositive: true
    property color accentColor: Theme.accent

    RowLayout {
        anchors.fill: parent
        anchors.margins: Theme.spacingLg
        spacing: Theme.spacingMd

        // Icon circle
        Rectangle {
            width: 44; height: 44; radius: 22
            color: Qt.rgba(root.accentColor.r, root.accentColor.g, root.accentColor.b, 0.15)

            Text {
                anchors.centerIn: parent
                text: root.icon
                font.pixelSize: 20
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4

            Text {
                text: root.label
                color: Theme.textMuted
                font.pixelSize: Theme.fontSm
            }

            Text {
                text: root.value
                color: Theme.textPrimary
                font.pixelSize: 24
                font.weight: Font.Bold
            }

            Text {
                text: root.change
                color: root.changePositive ? Theme.success : Theme.error
                font.pixelSize: Theme.fontXs
                font.weight: Font.Medium
            }
        }
    }
}
```

Usage:
```qml
StatCard {
    icon: "👥"; label: "Total Users"; value: "12,458"
    change: "+12.5%"; changePositive: true
    accentColor: "#6366f1"
}
```

---

## 9. Dashboard with Charts

### QtCharts in QML

```python
# main.py — Register QtCharts module
from PySide6.QtQml import qmlRegisterType
from PySide6.QtCharts import QChart, QLineSeries
# Import QtCharts in QML: import QtCharts
```

```qml
// components/LineChart.qml
import QtQuick 2.15
import QtCharts 2.15

Rectangle {
    radius: Theme.radiusLg
    color: Theme.surfaceCard

    ChartView {
        id: chart
        anchors.fill: parent
        anchors.margins: 8
        antialiasing: true
        backgroundColor: "transparent"
        legend.visible: false

        // Remove chart margins
        margins.top: 0; margins.bottom: 0
        margins.left: 0; margins.right: 0

        ValueAxis {
            id: axisX
            min: 0; max: 30
            gridVisible: false
            labelsVisible: false
            lineVisible: false
        }

        ValueAxis {
            id: axisY
            min: 0; max: 100
            gridVisible: true
            gridLineColor: Qt.rgba(1, 1, 1, 0.05)
            labelsVisible: true
            labelsColor: Theme.textMuted
            labelFormat: "%d"
            lineVisible: false
        }

        LineSeries {
            id: lineSeries
            axisX: axisX
            axisY: axisY
            color: Theme.accent
            width: 2

            // Points added dynamically from Python
        }

        AreaSeries {
            axisX: axisX
            axisY: axisY
            color: Qt.rgba(0.39, 0.4, 0.95, 0.1)  // accent at 10% opacity
            borderWidth: 0
            upperSeries: lineSeries
        }
    }

    // Function to update data
    function setData(points) {
        lineSeries.clear()
        for (var i = 0; i < points.length; i++) {
            lineSeries.append(i, points[i])
        }
    }
}
```

### Python → QML Chart Data

```python
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtQml import QQmlContext

class ChartDataBridge(QObject):
    dataReady = Signal(list)

    @Slot()
    def requestData(self):
        # Fetch/process data
        data = [10, 25, 15, 40, 35, 60, 45, 80, 70, 90]
        self.dataReady.emit(data)

# In main.py:
bridge = ChartDataBridge()
engine.rootContext().setContextProperty("chartBridge", bridge)
```

```qml
// In QML, connect signal:
Connections {
    target: chartBridge
    function onDataReady(points) { lineChart.setData(points) }
}

Component.onCompleted: chartBridge.requestData()
```

---

## 10. Settings Page with Toggles

```qml
// pages/SettingsPage.qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Page {
    background: Rectangle { color: Theme.canvas }

    Flickable {
        anchors.fill: parent
        contentHeight: settingsLayout.implicitHeight
        clip: true

        ColumnLayout {
            id: settingsLayout
            anchors.fill: parent
            anchors.margins: Theme.spacingXl
            spacing: Theme.spacingXl

            Text {
                text: "Settings"
                color: Theme.textPrimary
                font.pixelSize: Theme.fontXxl
                font.weight: Font.Bold
            }

            // Section: General
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                Text {
                    text: "General"
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontSm
                    font.weight: Font.Medium
                    Layout.bottomMargin: Theme.spacingMd
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: sectionContent.implicitHeight
                    radius: Theme.radiusLg
                    color: Theme.surfaceCard

                    ColumnLayout {
                        id: sectionContent
                        anchors.fill: parent
                        anchors.margins: Theme.spacingLg
                        spacing: 0

                        // Setting row: Dark Mode
                        RowLayout {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 56

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Text { text: "Dark Mode"; color: Theme.textPrimary; font.pixelSize: 14 }
                                Text { text: "Use dark theme throughout the app"; color: Theme.textMuted; font.pixelSize: 12 }
                            }

                            // Custom toggle switch
                            Rectangle {
                                id: toggleDark
                                width: 44; height: 24; radius: 12
                                color: toggleDark.checked ? Theme.accent : Theme.surfaceAlt
                                border.color: toggleDark.checked ? Theme.accent : Theme.textDisabled
                                border.width: 1
                                property bool checked: true

                                Behavior on color { ColorAnimation { duration: 150 } }

                                Rectangle {
                                    width: 18; height: 18; radius: 9
                                    anchors.verticalCenter: parent.verticalCenter
                                    x: toggleDark.checked ? parent.width - width - 3 : 3
                                    color: Theme.textPrimary

                                    Behavior on x { NumberAnimation { duration: 150; easing.type: Easing.InOutQuad } }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: toggleDark.checked = !toggleDark.checked
                                }
                            }
                        }

                        // Divider
                        Rectangle { Layout.fillWidth: true; height: 1; color: Qt.rgba(1,1,1,0.06) }

                        // Setting row: Notifications
                        RowLayout {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 56

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                Text { text: "Notifications"; color: Theme.textPrimary; font.pixelSize: 14 }
                                Text { text: "Receive push notifications"; color: Theme.textMuted; font.pixelSize: 12 }
                            }

                            Rectangle {
                                id: toggleNotif
                                width: 44; height: 24; radius: 12
                                color: toggleNotif.checked ? Theme.accent : Theme.surfaceAlt
                                property bool checked: false

                                Behavior on color { ColorAnimation { duration: 150 } }

                                Rectangle {
                                    width: 18; height: 18; radius: 9
                                    anchors.verticalCenter: parent.verticalCenter
                                    x: toggleNotif.checked ? parent.width - width - 3 : 3
                                    color: Theme.textPrimary
                                    Behavior on x { NumberAnimation { duration: 150; easing.type: Easing.InOutQuad } }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: toggleNotif.checked = !toggleNotif.checked
                                }
                            }
                        }
                    }
                }
            }

            // Section: Account (text fields)
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                Text {
                    text: "Account"
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontSm
                    font.weight: Font.Medium
                    Layout.bottomMargin: Theme.spacingMd
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: accountContent.implicitHeight
                    radius: Theme.radiusLg
                    color: Theme.surfaceCard

                    ColumnLayout {
                        id: accountContent
                        anchors.fill: parent
                        anchors.margins: Theme.spacingLg
                        spacing: Theme.spacingLg

                        ColumnLayout {
                            spacing: 6
                            Text { text: "Display Name"; color: Theme.textSecondary; font.pixelSize: 12 }
                            Rectangle {
                                Layout.fillWidth: true; height: 36; radius: Theme.radiusSm
                                color: Theme.surfaceAlt
                                border.color: Theme.textDisabled; border.width: 1

                                TextInput {
                                    anchors.fill: parent
                                    anchors.margins: 10
                                    color: Theme.textPrimary
                                    font.pixelSize: 14
                                    clip: true
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: "Enter your name"
                                        color: Theme.textDisabled
                                        font: parent.font
                                        visible: !parent.text && !parent.activeFocus
                                    }
                                }
                            }
                        }

                        // Save button
                        Rectangle {
                            Layout.alignment: Qt.AlignRight
                            width: 100; height: 36; radius: Theme.radiusSm
                            color: Theme.accent

                            Text {
                                anchors.centerIn: parent
                                text: "Save"
                                color: "white"
                                font.pixelSize: 14
                                font.weight: Font.Medium
                            }

                            MouseArea {
                                anchors.fill: parent
                                onClicked: { /* save logic */ }
                                hoverEnabled: true
                                onEntered: parent.color = Theme.accentHover
                                onExited: parent.color = Theme.accent
                            }
                        }
                    }
                }
            }
        }
    }
}
```

---

## 11. Toast Notifications

### Option A: pyqt-toast-notification (Python Widgets)

```bash
pip install pyqt-toast-notification
```

```python
from pyqttoast import Toast, ToastPreset

toast = Toast(self)
toast.setDuration(5000)
toast.setTitle('Success!')
toast.setText('Your post has been scheduled.')
toast.applyPreset(ToastPreset.SUCCESS_DARK)
toast.show()
```

### Option B: QtToastify (Pure QML)

```bash
# Drop files from https://github.com/mastercomdev/QtToastify into project
```

```qml
import "Toastify"  // or wherever you placed the files

Toastify {
    id: toast
    // Zero dependencies, pure QML/JS
}

// Show a toast:
function showToast(message) {
    toast.show(message, 3000)  // message, duration ms
}
```

### Option C: Custom QML Toast

```qml
// components/Toast.qml
import QtQuick 2.15
import QtQuick.Controls 2.15

Item {
    id: root
    anchors.fill: parent

    property string message: ""
    property string type: "success"  // success, error, warning, info

    function show(msg, toastType) {
        message = msg
        type = toastType || "success"
        toastRect.opacity = 1
        toastRect.y = parent.height - 80
        hideTimer.restart()
    }

    Rectangle {
        id: toastRect
        anchors.horizontalCenter: parent.horizontalCenter
        y: parent.height
        width: toastRow.implicitWidth + 32
        height: 44
        radius: Theme.radiusMd
        color: {
            switch(root.type) {
                case "success": return Qt.rgba(0.13, 0.77, 0.37, 0.9)
                case "error":   return Qt.rgba(0.94, 0.27, 0.27, 0.9)
                case "warning": return Qt.rgba(0.96, 0.62, 0.04, 0.9)
                default:        return Qt.rgba(0.39, 0.4, 0.95, 0.9)
            }
        }
        opacity: 0

        Behavior on opacity { NumberAnimation { duration: 250 } }
        Behavior on y { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }

        Row {
            id: toastRow
            anchors.centerIn: parent
            spacing: 8

            Text {
                text: root.type === "success" ? "✓" : root.type === "error" ? "✕" : "ℹ"
                color: "white"
                font.pixelSize: 16
                font.weight: Font.Bold
            }

            Text {
                text: root.message
                color: "white"
                font.pixelSize: 14
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    Timer {
        id: hideTimer
        interval: 3000
        onTriggered: {
            toastRect.opacity = 0
            toastRect.y = parent.height
        }
    }
}
```

---

## 12. Custom Scrollbar Styling

```qml
// QML ScrollBar customization
Flickable {
    id: flickable
    clip: true

    ScrollBar.vertical: ScrollBar {
        id: vbar
        active: true
        policy: ScrollBar.AsNeeded

        contentItem: Rectangle {
            implicitWidth: 6
            radius: 3
            color: vbar.pressed ? Theme.textMuted
                : vbar.hovered ? Qt.rgba(1, 1, 1, 0.2) : Qt.rgba(1, 1, 1, 0.1)

            Behavior on color { ColorAnimation { duration: 150 } }
        }

        background: Rectangle {
            implicitWidth: 6
            color: "transparent"
        }
    }
}
```

### Qt Widgets Scrollbar (QSS)

```css
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 0.15);
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(255, 255, 255, 0.25);
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}
```

---

## 13. Responsive Layout

### Qt 6.6+ Responsive Layouts (LayoutItemProxy)

```qml
import QtQuick 2.15
import QtQuick.Layouts 1.15

ColumnLayout {
    id: root
    // Use width breakpoints
    property bool isCompact: width < 768
    property bool isMedium: width >= 768 && width < 1024

    // Responsive grid
    GridLayout {
        Layout.fillWidth: true
        columns: root.isCompact ? 1 : root.isMedium ? 2 : 3
        columnSpacing: Theme.spacingLg
        rowSpacing: Theme.spacingLg

        // Cards automatically reflow
        StatCard { Layout.fillWidth: true }
        StatCard { Layout.fillWidth: true }
        StatCard { Layout.fillWidth: true }
    }
}
```

### Adaptive Sidebar

```qml
Sidebar {
    id: sidebar
    collapsed: root.width < 900  // Auto-collapse at narrow widths
}
```

---

## 14. Glassmorphism Effects

### QML Blur/Frosted Glass

```qml
import QtQuick 2.15
import QtGraphicalEffects 1.15  // or Qt5Compat.GraphicalEffects in Qt6

Rectangle {
    id: glassCard
    width: 300; height: 200
    radius: Theme.radiusXl
    color: Qt.rgba(0.1, 0.1, 0.15, 0.6)  // Semi-transparent
    border.color: Qt.rgba(1, 1, 1, 0.1)
    border.width: 1

    // Backdrop blur
    ShaderEffectSource {
        id: blurSource
        sourceItem: backgroundImage  // The item behind the glass
        sourceRect: Qt.rect(glassCard.x, glassCard.y, glassCard.width, glassCard.height)
        live: true
    }

    GaussianBlur {
        anchors.fill: parent
        source: blurSource
        radius: 20
        samples: 20
        transparentBorder: true
    }
}
```

> **Note:** For Qt6, use `import Qt5Compat.GraphicalEffects 1.15` (requires `qt5compat` module).

---

## 15. Font Loading (Inter)

### Method 1: FontLoader in QML

```qml
// fonts/FontLoader.qml
import QtQuick 2.15

QtObject {
    // Load from local file
    readonly property FontLoader interRegular: FontLoader {
        source: Qt.resolvedUrl("../../assets/fonts/Inter-Regular.ttf")
    }
    readonly property FontLoader interMedium: FontLoader {
        source: Qt.resolvedUrl("../../assets/fonts/Inter-Medium.ttf")
    }
    readonly property FontLoader interSemiBold: FontLoader {
        source: Qt.resolvedUrl("../../assets/fonts/Inter-SemiBold.ttf")
    }
    readonly property FontLoader interBold: FontLoader {
        source: Qt.resolvedUrl("../../assets/fonts/Inter-Bold.ttf")
    }

    // The font family name — use this everywhere
    readonly property string family: interRegular.name
}
```

### Method 2: Register in Python (Recommended)

```python
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtCore import QDir

# Add font directory
QFontDatabase.addApplicationFont("assets/fonts/Inter-Regular.ttf")
QFontDatabase.addApplicationFont("assets/fonts/Inter-Medium.ttf")
QFontDatabase.addApplicationFont("assets/fonts/Inter-SemiBold.ttf")
QFontDatabase.addApplicationFont("assets/fonts/Inter-Bold.ttf")

# Set as default
app = QGuiApplication(sys.argv)
font = QFont("Inter", 14)
app.setFont(font)
```

### Method 3: Qt Resource System

```qml
// In qml.qrc or CMakeLists.txt, embed fonts as resources
FontLoader {
    source: "qrc:/fonts/Inter-Regular.ttf"
}
```

### Using the Font in QML

```qml
Text {
    text: "Hello World"
    font.family: "Inter"
    font.pixelSize: 16
    font.weight: Font.Medium
    color: Theme.textPrimary
}
```

> **Important:** If FontLoader fails to load, the font name won't match. Always check `fontLoader.status === FontLoader.Ready` for debugging.

---

## 16. Material Design 3 in Qt/QML

### Official Qt Material Style (Qt 6.5+)

Qt Quick Controls ships with a Material style that was updated to **Material 3** in Qt 6.5.

```qml
// qtquickcontrols2.conf
[Material]
Theme=Dark
Accent=Teal
Primary=BlueGrey
Variant=Dense   // Desktop-sized controls (smaller, denser)
```

**Key M3 features in Qt:**
- `Material.roundedScale` — corner radius scale for controls
- `Material.containerStyle` — Filled/Outlined for text fields
- `Material.elevation` — shadow depth
- Rounded corners on Dialogs, Drawers, Menus
- New Switch visuals
- Outlined text fields and comboboxes

### Community: QmlMaterial (Full M3 Implementation)

[hypengw/QmlMaterial](https://github.com/hypengw/QmlMaterial) — Material Design 3 for QML, requires Qt 6.8+

```qml
import Qcm.Material as MD

MD.Text { text: 'hello world' }
```

> No dependency on `QtQuick.Controls` — uses only `QtQuick.Templates`.

### qt-material for Widgets

```bash
pip install qt-material
```

```python
from qt_material import apply_stylesheet, list_themes

# List available themes
print(list_themes())  # ['dark_amber.xml', 'dark_blue.xml', ...]

# Apply with custom colors
apply_stylesheet(app, theme='dark_teal.xml', extra={
    'density_scale': '-1',
    'font_family': 'Inter',
})
```

---

## 17. Qt Quick Controls 2 Custom Styling

### Approach 1: Inline Customization

```qml
Button {
    text: "Click Me"
    contentItem: Text {
        text: parent.text
        font: parent.font
        color: parent.down ? Theme.accentHover : Theme.textPrimary
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
        implicitWidth: 100; implicitHeight: 36
        radius: Theme.radiusSm
        color: parent.down ? Theme.accentHover : parent.hovered ? Theme.accent : Theme.accentMuted
        Behavior on color { ColorAnimation { duration: 150 } }
    }
}
```

### Approach 2: Custom Style Directory

Create a directory `MyStyle/` with QML files:

```
MyStyle/
├── qmldir
├── Button.qml
├── ScrollBar.qml
├── Switch.qml
└── ...
```

**qmldir:**
```
module MyStyle
Button 2.15 Button.qml
import QtQuick.Controls.Basic auto
```

**Button.qml:**
```qml
import QtQuick 2.15
import QtQuick.Templates 2.15 as T

T.Button {
    id: control
    implicitWidth: Math.max(implicitBackgroundWidth + leftInset + rightInset,
                            implicitContentWidth + leftPadding + rightPadding)
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset,
                             implicitContentHeight + topPadding + bottomPadding)

    contentItem: Text {
        text: control.text
        font: control.font
        color: control.enabled ? (control.down ? "#818cf8" : "#f0f0f5") : "#404050"
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        implicitWidth: 100; implicitHeight: 36
        radius: 8
        color: control.enabled
            ? (control.down ? "#312e81" : control.hovered ? "#1e1e2a" : "#6366f1")
            : "#1a1a25"
        Behavior on color { ColorAnimation { duration: 100 } }
    }
}
```

Run with: `python main.py -style MyStyle`

### Approach 3: Attached Properties for Theming

```qml
// Create custom attached properties for your app
pragma Singleton
import QtQuick 2.15

QtObject {
    id: myTheme

    property color accent: "#6366f1"
    property color surface: "#12121a"
    // ... (same as Theme singleton above)
}
```

---

## 18. Python ↔ QML Communication

### Context Properties (Simple)

```python
# main.py
from PySide6.QtCore import QObject, Signal, Slot, Property

class Backend(QObject):
    userNameChanged = Signal()

    def __init__(self):
        super().__init__()
        self._user_name = "User"

    @Property(str, notify=userNameChanged)
    def userName(self):
        return self._user_name

    @Slot(str)
    def setUserName(self, name):
        self._user_name = name
        self.userNameChanged.emit()

    @Slot(result=list)
    def getDashboardData(self):
        return [10, 25, 15, 40, 35, 60, 45, 80]

backend = Backend()
engine.rootContext().setContextProperty("backend", backend)
```

```qml
// In QML
Text { text: backend.userName }
Button {
    text: "Update"
    onClicked: backend.setUserName("Alice")
}
```

### QML Singleton Types (Recommended for Models)

Register with `@QmlElement` decorator:

```python
from PySide6.QtCore import QObject, Signal, Slot, Property
from PySide6.QtQml import QmlElement

QML_IMPORT_NAME = "XPST"
QML_IMPORT_MAJOR_VERSION = 1

@QmlElement
class DataManager(QObject):
    dataChanged = Signal()

    @Slot(result=list)
    def getPosts(self):
        return [{"title": "Post 1", "status": "published"}]
```

---

## 19. Recommended Project Structure

```
XPST/
├── main.py                    # App entry point
├── requirements.txt
├── pyproject.toml
│
├── qml/
│   ├── Main.qml              # Root window, layout, navigation
│   ├── Theme.qml              # Color/spacing singleton
│   ├── qmldir                 # QML module definition
│   │
│   ├── components/
│   │   ├── Card.qml
│   │   ├── StatCard.qml
│   │   ├── Toast.qml
│   │   ├── Toggle.qml
│   │   ├── Sidebar.qml
│   │   ├── TopBar.qml
│   │   └── SearchBar.qml
│   │
│   ├── pages/
│   │   ├── DashboardPage.qml
│   │   ├── PostsPage.qml
│   │   ├── SchedulePage.qml
│   │   ├── AnalyticsPage.qml
│   │   ├── SettingsPage.qml
│   │   └── AudiencePage.qml
│   │
│   └── charts/
│       ├── LineChart.qml
│       ├── BarChart.qml
│       └── DonutChart.qml
│
├── backend/
│   ├── __init__.py
│   ├── data_manager.py        # Python data layer
│   ├── api_client.py          # External API integration
│   └── models.py              # Data models
│
├── assets/
│   ├── fonts/
│   │   ├── Inter-Regular.ttf
│   │   ├── Inter-Medium.ttf
│   │   ├── Inter-SemiBold.ttf
│   │   └── Inter-Bold.ttf
│   ├── icons/
│   │   └── (SVG icons)
│   └── images/
│       └── (app images)
│
└── tests/
    └── ...
```

### qmldir

```
module XPST
singleton Theme Theme.qml
Card components/Card.qml
StatCard components/StatCard.qml
Toast components/Toast.qml
Sidebar components/Sidebar.qml
```

---

## 20. Color Palette Reference

### XPST Dark Theme (Linear/Raycast Inspired)

```
SURFACES
  Canvas (bg):        #0a0a0f
  Surface:            #12121a
  Surface Alt:        #1a1a25
  Surface Card:       #1e1e2a
  Surface Hover:      #252535

TEXT
  Primary:            #f0f0f5
  Secondary:          #a0a0b0
  Muted:              #6b6b80
  Disabled:           #404050

ACCENT (Indigo)
  Primary:            #6366f1
  Hover:              #818cf8
  Muted/Subtle:       #312e81

SEMANTIC
  Success:            #22c55e
  Warning:            #f59e0b
  Error:              #ef4444
  Info:               #3b82f6

BORDERS
  Subtle:             rgba(255, 255, 255, 0.06)
  Default:            rgba(255, 255, 255, 0.10)
  Strong:             rgba(255, 255, 255, 0.15)
```

### Alternative Accent Colors

```
Blue:    #3b82f6  (trust, professional)
Purple:  #8b5cf6  (creative, premium)
Teal:    #14b8a6  (fresh, growth)
Rose:    #f43f5e  (energy, attention)
Amber:   #f59e0b  (warmth, warning)
Green:   #22c55e  (success, nature)
```

---

## Summary of Key Design Decisions

1. **QML over Qt Widgets** — Better for modern, animated, custom-styled UIs
2. **Custom dark theme** (not Material style) — Full control, Linear/Raycast aesthetic
3. **Inter font** — Clean, modern, great readability at all sizes
4. **8px spacing grid** — Consistent rhythm throughout the UI
5. **4-level surface ladder** — Depth without shadows (Raycast pattern)
6. **Singleton Theme.qml** — Single source of truth for all design tokens
7. **StackView for navigation** — Smooth transitions between pages
8. **Component-based architecture** — Reusable Card, StatCard, Toast, Toggle components
9. **Property bindings** — Let QML handle automatic UI updates
10. **Python backend via signals/slots** — Clean separation of concerns

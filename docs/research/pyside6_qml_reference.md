# PySide6 QML Reference Document
## Building XPST: A Modern Social Media Cross-Posting Desktop App

*Research compiled for migration from NiceGUI to PySide6 QML*

---

## Table of Contents

1. [PySide6 vs PyQt6](#1-pyside6-vs-pyqt6)
2. [QML vs Qt Widgets](#2-qml-vs-qt-widgets)
3. [Installation](#3-installation)
4. [QML Project Structure](#4-qml-project-structure)
5. [Loading QML Files from Python](#5-loading-qml-files-from-python)
6. [Python-QML Communication](#6-python-qml-communication)
7. [Model-View Pattern in QML](#7-model-view-pattern-in-qml)
8. [Theming in QML](#8-theming-in-qml)
9. [Charts in QML](#9-charts-in-qml)
10. [Sidebar Navigation](#10-sidebar-navigation)
11. [System Tray Integration](#11-system-tray-integration)
12. [Native File Dialogs](#12-native-file-dialogs)
13. [Animations in QML](#13-animations-in-qml)
14. [Qt Resource System (.qrc)](#14-qt-resource-system-qrc)
15. [Packaging PySide6 Apps](#15-packaging-pyside6-apps)
16. [Best Practices Summary](#16-best-practices-summary)
17. [XPST Architecture Recommendation](#17-xpst-architecture-recommendation)

---

## 1. PySide6 vs PyQt6

### Quick Verdict: **Use PySide6**

Both are Python bindings for Qt 6 with 99.9% identical APIs. The choice comes down to licensing.

| Feature | PySide6 | PyQt6 |
|---------|---------|-------|
| **Developer** | Qt Company (official) | Riverbank Computing |
| **License** | **LGPL** | GPL or Commercial ($) |
| **Implication** | Can use in closed-source apps | Must open-source OR buy license |
| **Python API** | `Signal`, `Slot` decorators | `pyqtSignal`, `pyqtSlot` |
| **Enum style** | Both long and short forms work | Fully qualified only |
| **Snake case** | `__feature__` import available | Not available |
| **UI files** | `QUiLoader` | `uic.loadUi()` |
| **Official support** | Qt Company directly | Third party |

### Why PySide6 for XPST

- **LGPL license**: No obligation to open-source the application. Safe for commercial/closed-source distribution.
- **Official Qt binding**: Maintained by the Qt Company, same entity that develops Qt.
- **Pythonic features**: `snake_case` and `true_property` via `__feature__` imports:
  ```python
  from __feature__ import snake_case, true_property
  table.column_count = 2  # Instead of table.setColumnCount(2)
  button.enabled = False  # Instead of button.setEnabled(False)
  ```
- **Growing adoption**: Enterprise and commercial projects are moving to PySide6 for licensing simplicity.

### Key Technical Differences

**Signals/Slots:**
```python
# PySide6
from PySide6.QtCore import Signal, Slot

class Backend(QObject):
    data_ready = Signal(str)
    
    @Slot()
    def fetch_data(self):
        ...

# PyQt6 equivalent
from PyQt6.QtCore import pyqtSignal, pyqtSlot
```

**Enums:**
```python
# PySide6 - both work
Qt.ItemDataRole.DisplayRole
Qt.AlignLeft  # Legacy shortcut

# PyQt6 - fully qualified only
Qt.ItemDataRole.DisplayRole
```

---

## 2. QML vs Qt Widgets

### Recommendation: **QML for XPST**

| Aspect | QML (Qt Quick) | Qt Widgets |
|--------|---------------|------------|
| **UI paradigm** | Declarative, modern | Imperative, traditional |
| **Styling** | Full custom, Material Design built-in | QSS stylesheets, limited |
| **Animations** | First-class support | Manual, complex |
| **Touch/mobile** | Native support | Not designed for touch |
| **Learning curve** | Steeper (new language) | Gentler (Python-only) |
| **Custom rendering** | Easy with Canvas/shapes | QPainter overrides |
| **Property binding** | Core strength | Not available |
| **Community assets** | Growing | Very large (mature) |
| **Best for** | Modern, animated, beautiful UIs | Traditional desktop apps |

### Why QML for XPST

1. **Modern UI out of the box**: Material Design controls, dark themes, smooth animations
2. **Property bindings**: Change a color in one place, the whole UI updates
3. **Declarative**: Describe *what* the UI looks like, not *how* to build it
4. **Separation of concerns**: QML for UI, Python for business logic
5. **Cross-platform reach**: Could extend to mobile later

### Real-World Comparison (from "Three Dashboards, One Weekend")

Adding a new page to a dashboard app:
- Qt Widgets C++: ~5 files, ~80 lines
- Qt Widgets PySide6: ~3 files, ~40 lines
- **Qt Quick QML: ~4 files, ~30 lines** (winner)

The QML approach excels at theming via property bindings — changing one `Theme.qml` singleton propagates colors throughout the entire UI.

---

## 3. Installation

### Basic Installation
```bash
pip install PySide6
```

This installs:
- `PySide6` (meta-package)
- `PySide6-Essentials` (core Qt modules)
- `PySide6-Addons` (additional Qt modules including QtCharts, QtMultimedia, etc.)
- `shiboken6` (binding utility)

### With Addons (for Charts, Multimedia, etc.)
```bash
pip install PySide6-Addons
```

### For Development with Virtual Environment
```bash
# Using venv
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows
pip install PySide6

# Using uv (faster)
uv venv
source .venv/bin/activate
uv pip install PySide6
```

### Verify Installation
```python
from PySide6.QtCore import __version__
print(__version__)
```

### Included Tools
PySide6 installs these CLI tools:
- `pyside6-designer` — Visual UI designer
- `pyside6-uic` — Convert .ui files to Python
- `pyside6-rcc` — Compile .qrc resource files
- `pyside6-qml` — QML runtime for prototyping
- `pyside6-lupdate` — Translation tool

---

## 4. QML Project Structure

### Recommended Structure for XPST

```
xpst/
├── main.py                    # Application entry point
├── backend/
│   ├── __init__.py
│   ├── app_controller.py      # Main application controller (QObject)
│   ├── post_manager.py        # Social media posting logic
│   ├── account_manager.py     # Account management
│   ├── analytics.py           # Analytics/data models
│   └── models.py              # QAbstractListModel subclasses
├── qml/
│   ├── main.qml               # Root application window
│   ├── Theme.qml              # Theme singleton (colors, fonts)
│   ├── components/
│   │   ├── Sidebar.qml        # Sidebar navigation
│   │   ├── StatCard.qml       # Reusable stat card
│   │   ├── PlatformIcon.qml   # Platform icons
│   │   └── CustomButton.qml   # Styled button
│   ├── pages/
│   │   ├── DashboardPage.qml  # Analytics dashboard
│   │   ├── ComposePage.qml    # Post composer
│   │   ├── SchedulePage.qml   # Post scheduler
│   │   ├── AccountsPage.qml   # Account management
│   │   └── SettingsPage.qml   # App settings
│   └── dialogs/
│       ├── FilePickerDialog.qml
│       └── ConfirmDialog.qml
├── assets/
│   ├── icons/                 # SVG icons
│   ├── images/                # Images
│   └── fonts/                 # Custom fonts
├── resources.qrc              # Qt resource file
├── resources.py               # Compiled resources (generated)
└── requirements.txt
```

### Why This Structure

- **`backend/`**: All Python logic lives here, completely separate from UI
- **`qml/`**: All QML UI files, organized by function
- **`qml/Theme.qml`**: Single source of truth for colors, fonts, spacing
- **`qml/components/`**: Reusable UI components
- **`qml/pages/`**: Each major view is a separate page
- **`assets/`**: Static resources, bundled via .qrc

---

## 5. Loading QML Files from Python

### Method 1: QQmlApplicationEngine (Recommended)

```python
import sys
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

app = QGuiApplication(sys.argv)
engine = QQmlApplicationEngine()

# Load QML file
engine.load('qml/main.qml')

# Check for errors
if not engine.rootObjects():
    print("Error: Failed to load QML file")
    sys.exit(-1)

engine.quit.connect(app.quit)
sys.exit(app.exec())
```

### Method 2: With Context Properties (Passing Python Objects)

```python
import sys
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

app = QGuiApplication(sys.argv)
engine = QQmlApplicationEngine()

# Create backend objects
controller = AppController()
post_manager = PostManager()

# Expose to QML via context properties
context = engine.rootContext()
context.setContextProperty("controller", controller)
context.setContextProperty("postManager", post_manager)

engine.load('qml/main.qml')

if not engine.rootObjects():
    sys.exit(-1)

engine.quit.connect(app.quit)
sys.exit(app.exec())
```

### Method 3: With QML Module Registration (@QmlElement)

```python
# backend/models.py
from PySide6.QtCore import QObject, Slot, Property, Signal
from PySide6.QtQml import QmlElement

QML_IMPORT_NAME = "Xpst.Models"
QML_IMPORT_MAJOR_VERSION = 1

@QmlElement
class PostManager(QObject):
    @Slot(str, result=bool)
    def submit_post(self, content: str) -> bool:
        # Process post...
        return True
```

```qml
// main.qml
import Xpst.Models 1.0

PostManager {
    id: postManager
}

Button {
    onClicked: {
        let success = postManager.submitPost("Hello World")
    }
}
```

### Method 4: Loading from Resource System

```python
# After compiling resources: pyside6-rcc resources.qrc -o resources_rc.py
import resources_rc  # Registers resources

engine.load(':/qml/main.qml')  # Resource path with :/ prefix
```

### Loading from String (for testing)

```python
engine.loadData(b"""
import QtQuick 2.15
import QtQuick.Controls 2.15

ApplicationWindow {
    visible: true
    width: 400
    height: 300
    title: "Test"
    Text { text: "Hello"; anchors.centerIn: parent }
}
""")
```

---

## 6. Python-QML Communication

### Overview of Methods

| Method | Direction | Use Case |
|--------|-----------|----------|
| `@Slot` + `@Property` | Python → QML | Expose data to UI |
| `Signal` + `Connections` | Python → QML | Notify UI of changes |
| `setContextProperty` | Python → QML | Pass objects to QML |
| `@QmlElement` | Python → QML | Register Python types in QML |
| QML `signal` | QML → Python | UI events to backend |
| `setProperty()` | Python → QML | Set QML properties from Python |

### Method 1: Slots (Call Python from QML)

```python
# Python
from PySide6.QtCore import QObject, Slot

class Backend(QObject):
    @Slot(str)
    def post_to_all_platforms(self, content: str):
        print(f"Posting: {content}")
        # ... posting logic ...
    
    @Slot(str, result=str)
    def format_content(self, content: str) -> str:
        return content.strip().upper()
    
    @Slot(result=bool)
    def is_connected(self) -> bool:
        return True
```

```qml
// QML
Button {
    text: "Post"
    onClicked: backend.post_to_all_platforms(textField.text)
}

Text {
    text: backend.formatContent(rawText)
}

Image {
    visible: backend.isConnected()
    source: "connected.png"
}
```

### Method 2: Properties (Expose Data to QML)

```python
from PySide6.QtCore import QObject, Property, Signal

class AppController(QObject):
    def __init__(self):
        super().__init__()
        self._post_count = 0
        self._status = "Ready"
    
    # --- Post Count Property ---
    post_count_changed = Signal()
    
    def _get_post_count(self):
        return self._post_count
    
    def _set_post_count(self, value):
        if self._post_count != value:
            self._post_count = value
            self.post_count_changed.emit()
    
    post_count = Property(int, _get_post_count, _set_post_count, 
                          notify=post_count_changed)
    
    # --- Status Property (read-only) ---
    status_changed = Signal()
    
    @Property(str, notify=status_changed)
    def status(self):
        return self._status
    
    def _update_status(self, new_status):
        self._status = new_status
        self.status_changed.emit()
```

```qml
// QML - properties auto-bind
Text {
    text: "Posts today: " + controller.postCount
}

Text {
    text: controller.status
    color: controller.status === "Ready" ? "green" : "orange"
}
```

### Method 3: Signals (Notify QML from Python)

```python
from PySide6.QtCore import QObject, Signal, Slot

class PostManager(QObject):
    # Define signals
    post_started = Signal(str)          # platform name
    post_completed = Signal(str, bool)  # platform name, success
    post_failed = Signal(str, str)      # platform name, error message
    
    @Slot(str, list)
    def cross_post(self, content, platforms):
        for platform in platforms:
            self.post_started.emit(platform)
            try:
                # ... post logic ...
                self.post_completed.emit(platform, True)
            except Exception as e:
                self.post_failed.emit(platform, str(e))
```

```qml
// QML
Connections {
    target: postManager
    
    function onPostStarted(platform) {
        statusText.text = "Posting to " + platform + "..."
    }
    
    function onPostCompleted(platform, success) {
        if (success) {
            statusText.text = "✓ Posted to " + platform
        }
    }
    
    function onPostFailed(platform, error) {
        statusText.text = "✗ Failed on " + platform + ": " + error
    }
}
```

### Method 4: QML Signals to Python

```python
# Connect to QML signals from Python
root = engine.rootObjects()[0]

# Find QML object by objectName
button = root.findChild(QObject, "postButton")
button.clicked.connect(lambda: print("Button clicked in QML"))

# Connect custom QML signal
root.customSignal.connect(my_python_function)
```

```qml
// QML - define custom signal
Rectangle {
    objectName: "postContainer"
    signal postRequested(string content, var platforms)
    
    Button {
        objectName: "postButton"
        onClicked: parent.postRequested(textField.text, ["youtube", "twitter"])
    }
}
```

### Method 5: setProperty (Quick and Simple)

```python
# Set QML property from Python
engine.rootObjects()[0].setProperty('currTime', '12:30:00')
engine.rootObjects()[0].setProperty('backend', backend_instance)
```

### Signal Naming Convention

| Python Signal | QML Handler |
|---------------|-------------|
| `updated` | `onUpdated` |
| `data_ready` | `onData_ready` |
| `postCompleted` | `onPostCompleted` |

---

## 7. Model-View Pattern in QML

### When to Use

- **Simple static data**: Use QML `ListModel` directly
- **Dynamic data from Python**: Use `QAbstractListModel`
- **Large datasets**: Use `QAbstractListModel` with proper notifications

### Complete Example: Social Media Posts List

#### Python Model

```python
from enum import IntEnum, auto
from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Slot, Signal

class PostRoles(IntEnum):
    PLATFORM = Qt.UserRole
    CONTENT = auto()
    TIMESTAMP = auto()
    STATUS = auto()      # "draft", "scheduled", "posted", "failed"
    MEDIA_PATH = auto()

_role_names = {
    PostRoles.PLATFORM: b'platform',
    PostRoles.CONTENT: b'content',
    PostRoles.TIMESTAMP: b'timestamp',
    PostRoles.STATUS: b'status',
    PostRoles.MEDIA_PATH: b'mediaPath',
}

class PostListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._posts = []
    
    def roleNames(self):
        return _role_names
    
    def rowCount(self, parent=QModelIndex()):
        return len(self._posts)
    
    def data(self, index, role):
        if not index.isValid() or index.row() >= len(self._posts):
            return None
        post = self._posts[index.row()]
        return post.get(role)
    
    # --- Modifying the Model ---
    
    @Slot(str, str, str)
    def addPost(self, platform, content, status="draft"):
        row = len(self._posts)
        self.beginInsertRows(QModelIndex(), row, row)
        self._posts.append({
            PostRoles.PLATFORM: platform,
            PostRoles.CONTENT: content,
            PostRoles.TIMESTAMP: "2026-01-01 12:00",
            PostRoles.STATUS: status,
            PostRoles.MEDIA_PATH: "",
        })
        self.endInsertRows()
    
    @Slot(int)
    def removePost(self, row):
        if 0 <= row < len(self._posts):
            self.beginRemoveRows(QModelIndex(), row, row)
            self._posts.pop(row)
            self.endRemoveRows()
    
    @Slot(int, str, str)
    def updatePostStatus(self, row, new_status, timestamp=""):
        if 0 <= row < len(self._posts):
            self._posts[row][PostRoles.STATUS] = new_status
            if timestamp:
                self._posts[row][PostRoles.TIMESTAMP] = timestamp
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [PostRoles.STATUS, PostRoles.TIMESTAMP])
```

#### Exposing Model to QML

```python
# In main.py
class Controller(QObject):
    def __init__(self):
        super().__init__()
        self._post_model = PostListModel()
    
    post_model_changed = Signal()
    
    @Property(QObject, notify=post_model_changed)
    def postModel(self):
        return self._post_model

controller = Controller()
engine.rootContext().setContextProperty("controller", controller)
```

#### QML ListView

```qml
import QtQuick 2.15
import QtQuick.Controls 2.15

ListView {
    id: postList
    model: controller.postModel
    spacing: 8
    
    delegate: Rectangle {
        width: postList.width
        height: 80
        radius: 8
        color: status === "posted" ? "#1a3d1a" :
               status === "failed" ? "#3d1a1a" :
               status === "scheduled" ? "#1a1a3d" : "#2a2a2a"
        
        Column {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 4
            
            Text {
                text: platform
                color: platform === "youtube" ? "#ff0000" :
                       platform === "twitter" ? "#1da1f2" :
                       platform === "instagram" ? "#e1306c" :
                       "#ff00ff"  // tiktok
                font.bold: true
            }
            
            Text {
                text: content
                color: "#ffffff"
                elide: Text.ElideRight
                width: parent.width
            }
            
            Text {
                text: status + " • " + timestamp
                color: "#888888"
                font.pixelSize: 11
            }
        }
        
        MouseArea {
            anchors.fill: parent
            onClicked: postList.currentIndex = index
        }
    }
    
    highlight: Rectangle {
        color: "#3a3a3a"
        radius: 8
    }
}
```

### Using QML ListModel (Simple Cases)

For simple, static-ish data that doesn't need Python processing:

```qml
ListModel {
    id: platformModel
    
    ListElement {
        name: "YouTube"
        icon: "youtube.svg"
        color: "#ff0000"
        enabled: true
    }
    ListElement {
        name: "Instagram"
        icon: "instagram.svg"
        color: "#e1306c"
        enabled: true
    }
    ListElement {
        name: "X (Twitter)"
        icon: "twitter.svg"
        color: "#1da1f2"
        enabled: false
    }
    ListElement {
        name: "TikTok"
        icon: "tiktok.svg"
        color: "#ff00ff"
        enabled: true
    }
}

ListView {
    model: platformModel
    delegate: Row {
        Image { source: icon }
        Text { text: name; color: model.color }
        Switch { checked: model.enabled }
    }
}
```

---

## 8. Theming in QML

### Option A: Material Design Style (Built-in, Recommended for QML)

Qt Quick Controls has **built-in Material Design support** that works perfectly with QML.

#### Enabling Material Style

**Method 1: In QML (Recommended)**
```qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15

ApplicationWindow {
    visible: true
    width: 1200
    height: 800
    
    Material.theme: Material.Dark
    Material.accent: Material.Purple
    Material.primary: Material.Indigo
    Material.foreground: Material.Grey
    Material.background: "#1e1e2e"
}
```

**Method 2: Configuration File (`qtquickcontrols2.conf`)**
```ini
[Material]
Theme=Dark
Variant=Dense
Accent=Teal
Primary=BlueGrey
Background=#1e1e2e
```

**Method 3: Environment Variables**
```bash
export QT_QUICK_CONTROLS_MATERIAL_THEME=Dark
export QT_QUICK_CONTROLS_MATERIAL_ACCENT=Teal
export QT_QUICK_CONTROLS_MATERIAL_VARIANT=Dense
```

#### Material Properties Reference

| Property | Description | Default |
|----------|-------------|---------|
| `Material.theme` | `Light`, `Dark`, or `System` | `Light` |
| `Material.primary` | Background color for ToolBar, etc. | `Indigo` |
| `Material.accent` | Highlight/accent color | `Pink` |
| `Material.foreground` | Foreground/text color | Theme-dependent |
| `Material.background` | Background color | Theme-dependent |
| `Material.elevation` | Shadow depth (integer) | Control-specific |
| `Material.roundedScale` | Corner rounding | Default rounded |

#### Material Color Helper
```qml
// Get specific shade of a Material color
Rectangle {
    color: Material.color(Material.Red, Material.Shade200)
}
```

#### Pre-defined Colors
`Material.Red`, `Material.Pink`, `Material.Purple`, `Material.DeepPurple`, `Material.Indigo`, `Material.Blue`, `Material.LightBlue`, `Material.Cyan`, `Material.Teal`, `Material.Green`, `Material.LightGreen`, `Material.Lime`, `Material.Yellow`, `Material.Amber`, `Material.Orange`, `Material.DeepOrange`, `Material.Brown`, `Material.Grey`, `Material.BlueGrey`

#### Desktop-Optimized Material Variant
```qml
// For desktop: use Dense variant (smaller controls, better for mouse)
ApplicationWindow {
    Material.theme: Material.Dark
    Material.accent: Material.Cyan
    // Dense variant set via conf file or env var
}
```

### Option B: Custom Theme Singleton (Best for Full Control)

```qml
// Theme.qml — singleton
pragma Singleton
import QtQuick 2.15

QtObject {
    // Background colors
    readonly property color bgPrimary: "#0f0f1a"
    readonly property color bgSecondary: "#1a1a2e"
    readonly property color bgTertiary: "#252540"
    readonly property color bgCard: "#1e1e35"
    readonly property color bgHover: "#2a2a4a"
    
    // Text colors
    readonly property color textPrimary: "#e0e0ff"
    readonly property color textSecondary: "#8888aa"
    readonly property color textMuted: "#555577"
    
    // Accent colors (per platform)
    readonly property color youtubeRed: "#ff0000"
    readonly property color instagramPink: "#e1306c"
    readonly property color twitterBlue: "#1da1f2"
    readonly property color tiktokCyan: "#00f2ea"
    readonly property color accentPurple: "#7c3aed"
    
    // Status colors
    readonly property color success: "#22c55e"
    readonly property color warning: "#f59e0b"
    readonly property color error: "#ef4444"
    readonly property color info: "#3b82f6"
    
    // Spacing
    readonly property int spacingXs: 4
    readonly property int spacingSm: 8
    readonly property int spacingMd: 16
    readonly property int spacingLg: 24
    readonly property int spacingXl: 32
    
    // Border radius
    readonly property int radiusSm: 6
    readonly property int radiusMd: 12
    readonly property int radiusLg: 16
    readonly property int radiusFull: 9999
    
    // Font sizes
    readonly property int fontXs: 11
    readonly property int fontSm: 13
    readonly property int fontMd: 15
    readonly property int fontLg: 20
    readonly property int fontXl: 28
    readonly property int font2xl: 36
    
    // Dark mode toggle
    property bool isDark: true
}
```

Register as singleton in `qmldir`:
```
// qml/qmldir
singleton Theme Theme.qml
```

Use everywhere:
```qml
Rectangle {
    color: Theme.bgCard
    radius: Theme.radiusMd
    
    Text {
        text: "Analytics"
        color: Theme.textPrimary
        font.pixelSize: Theme.fontLg
    }
}
```

### Option C: qt-material Library (For Qt Widgets, Not QML)

The `qt-material` library provides Material Design themes for Qt Widgets apps (not QML):
```bash
pip install qt-material
```

```python
from qt_material import apply_stylesheet
apply_stylesheet(app, theme='dark_teal.xml')
```

**Note:** This is for Qt Widgets, not QML. For QML, use the built-in Material style or custom Theme.qml singleton.

### Dark/Light Mode Toggle

```qml
// In Theme.qml
property bool isDark: true

// Derived colors that change with mode
readonly property color bgPrimary: isDark ? "#0f0f1a" : "#ffffff"
readonly property color textPrimary: isDark ? "#e0e0ff" : "#1a1a2e"

// Toggle function
function toggleTheme() {
    isDark = !isDark
}
```

```qml
// Settings page
Switch {
    text: "Dark Mode"
    checked: Theme.isDark
    onToggled: Theme.toggleTheme()
}
```

---

## 9. Charts in QML

### Option A: QtCharts (Native QML Charts, Recommended)

QtCharts provides native QML chart types. Requires `PySide6-Addons`.

```bash
pip install PySide6-Addons
```

#### Python Setup for QtCharts

```python
import sys
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

app = QGuiApplication(sys.argv)
engine = QQmlApplicationEngine()

# Expose data controller
controller = ChartController()
engine.rootContext().setContextProperty("chartController", controller)

engine.load('qml/main.qml')
sys.exit(app.exec())
```

#### QML Chart Example: Analytics Line Chart

```qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtCharts 2.15

ChartView {
    id: chartView
    title: "Post Performance"
    antialiasing: true
    backgroundColor: Theme.bgCard
    titleColor: Theme.textPrimary
    titleFont.pixelSize: Theme.fontLg
    legend.visible: true
    legend.labelColor: Theme.textSecondary
    
    // Enable animations
    animationOptions: ChartView.SeriesAnimations
    
    ValueAxis {
        id: axisX
        min: 1
        max: 30
        titleText: "Day"
        titleBrush: Theme.textSecondary
        labelsColor: Theme.textSecondary
        gridLineColor: Theme.bgTertiary
    }
    
    ValueAxis {
        id: axisY
        min: 0
        max: 1000
        titleText: "Engagement"
        titleBrush: Theme.textSecondary
        labelsColor: Theme.textSecondary
        gridLineColor: Theme.bgTertiary
    }
    
    LineSeries {
        id: youtubeSeries
        name: "YouTube"
        color: Theme.youtubeRed
        axisX: axisX
        axisY: axisY
        width: 2
    }
    
    LineSeries {
        id: instagramSeries
        name: "Instagram"
        color: Theme.instagramPink
        axisX: axisX
        axisY: axisY
        width: 2
    }
    
    LineSeries {
        id: twitterSeries
        name: "X"
        color: Theme.twitterBlue
        axisX: axisX
        axisY: axisY
        width: 2
    }
    
    // Update from Python
    Component.onCompleted: {
        chartController.dataReady.connect(function(points) {
            youtubeSeries.clear()
            for (let p of points) {
                youtubeSeries.append(p.x, p.y)
            }
        })
    }
}
```

#### Python Chart Data Provider

```python
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtCore import QPointF

class ChartController(QObject):
    dataReady = Signal(list)
    
    @Slot()
    def loadAnalytics(self):
        # Fetch data from APIs
        points = [QPointF(i, i * 10 + 50) for i in range(1, 31)]
        self.dataReady.emit(points)
```

#### Available Chart Types in QML

- `LineSeries` — Line charts
- `SplineSeries` — Smooth line charts
- `BarSeries` / `BarSet` — Bar charts
- `PieSeries` / `PieSlice` — Pie/donut charts
- `ScatterSeries` — Scatter plots
- `AreaSeries` — Area charts
- `PolarChartView` — Polar/radar charts

#### Donut Chart Example

```qml
ChartView {
    title: "Platform Distribution"
    antialiasing: true
    backgroundColor: "transparent"
    
    PieSeries {
        id: platformPie
        
        PieSlice {
            label: "YouTube"
            value: 35
            color: Theme.youtubeRed
            labelVisible: true
            labelColor: Theme.textPrimary
        }
        PieSlice {
            label: "Instagram"
            value: 30
            color: Theme.instagramPink
            labelVisible: true
            labelColor: Theme.textPrimary
        }
        PieSlice {
            label: "X"
            value: 20
            color: Theme.twitterBlue
            labelVisible: true
            labelColor: Theme.textPrimary
        }
        PieSlice {
            label: "TikTok"
            value: 15
            color: Theme.tiktokCyan
            labelVisible: true
            labelColor: Theme.textPrimary
        }
    }
    
    // Make it a donut
    Component.onCompleted: {
        platformPie.holeSize = 0.5
    }
}
```

### Option B: Embedding Matplotlib/Plotly (For Complex Charts)

For charts not easily done in QtCharts, render matplotlib/plotly to image and display in QML:

```python
import io
import base64
from PySide6.QtCore import QObject, Signal, Slot, QUrl
from PySide6.QtGui import QImage
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

class ChartRenderer(QObject):
    chartReady = Signal(str)  # base64 encoded image
    
    @Slot(list, list)
    def renderChart(self, x_data, y_data):
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(x_data, y_data)
        ax.set_facecolor('#1e1e2e')
        fig.patch.set_facecolor('#1e1e2e')
        
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        plt.close(fig)
        
        b64 = base64.b64encode(buf.getvalue()).decode()
        self.chartReady.emit(f"data:image/png;base64,{b64}")
```

```qml
Image {
    id: chartImage
    fillMode: Image.PreserveAspectFit
    
    Connections {
        target: chartRenderer
        function onChartReady(dataUrl) {
            chartImage.source = dataUrl
        }
    }
}
```

### Recommendation for XPST

**Use QtCharts** for:
- Line charts (engagement over time)
- Pie/donut charts (platform distribution)
- Bar charts (post counts by platform)

**Use embedded matplotlib** only for:
- Complex statistical visualizations
- Charts that need features QtCharts lacks

---

## 10. Sidebar Navigation

### Architecture: Sidebar + StackLayout

The most common pattern for modern desktop apps with a sidebar.

#### Main.qml

```qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15

ApplicationWindow {
    id: root
    visible: true
    width: 1200
    height: 800
    title: "XPST - Cross-Post"
    
    Material.theme: Material.Dark
    Material.accent: Material.Cyan
    
    RowLayout {
        anchors.fill: parent
        spacing: 0
        
        // === SIDEBAR ===
        Rectangle {
            id: sidebar
            Layout.preferredWidth: 220
            Layout.fillHeight: true
            color: Theme.bgSecondary
            
            ColumnLayout {
                anchors.fill: parent
                spacing: 0
                
                // App Logo/Title
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 60
                    color: "transparent"
                    
                    Text {
                        anchors.centerIn: parent
                        text: "⚡ XPST"
                        font.pixelSize: 22
                        font.bold: true
                        color: Theme.accentPurple
                    }
                }
                
                // Navigation Buttons
                Repeater {
                    model: [
                        { text: "Dashboard", icon: "📊", page: 0 },
                        { text: "Compose",   icon: "✏️", page: 1 },
                        { text: "Schedule",  icon: "📅", page: 2 },
                        { text: "Accounts",  icon: "👤", page: 3 },
                        { text: "Settings",  icon: "⚙️", page: 4 },
                    ]
                    
                    delegate: Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 48
                        
                        color: stackLayout.currentIndex === modelData.page 
                               ? Theme.bgHover : "transparent"
                        
                        radius: 8
                        
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 16
                            spacing: 12
                            
                            Text {
                                text: modelData.icon
                                font.pixelSize: 18
                            }
                            
                            Text {
                                text: modelData.text
                                color: stackLayout.currentIndex === modelData.page
                                       ? Theme.textPrimary : Theme.textSecondary
                                font.pixelSize: 14
                                font.weight: stackLayout.currentIndex === modelData.page
                                             ? Font.DemiBold : Font.Normal
                            }
                        }
                        
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: stackLayout.currentIndex = modelData.page
                        }
                        
                        // Hover effect
                        states: State {
                            name: "hovered"
                            when: sidebarMouseArea.containsMouse
                            PropertyChanges { target: parent; color: Theme.bgHover }
                        }
                    }
                }
                
                Item { Layout.fillHeight: true }  // Spacer
            }
        }
        
        // Divider
        Rectangle {
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            color: Theme.bgTertiary
        }
        
        // === MAIN CONTENT ===
        StackLayout {
            id: stackLayout
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: 0
            
            DashboardPage {}
            ComposePage {}
            SchedulePage {}
            AccountsPage {}
            SettingsPage {}
        }
    }
}
```

#### DashboardPage.qml (Example Page)

```qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Page {
    background: Rectangle { color: Theme.bgPrimary }
    
    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        
        ColumnLayout {
            width: parent.width
            spacing: Theme.spacingLg
            anchors.margins: Theme.spacingLg
            
            // Header
            Text {
                text: "Dashboard"
                font.pixelSize: Theme.fontXl
                font.bold: true
                color: Theme.textPrimary
            }
            
            // Stat Cards Row
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingMd
                
                Repeater {
                    model: [
                        { title: "Total Posts", value: "1,234", change: "+12%", positive: true },
                        { title: "Engagement", value: "45.2K", change: "+8%", positive: true },
                        { title: "Followers", value: "12.1K", change: "+3%", positive: true },
                        { title: "Failed", value: "3", change: "-2", positive: false },
                    ]
                    
                    delegate: Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 100
                        color: Theme.bgCard
                        radius: Theme.radiusMd
                        
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: Theme.spacingMd
                            spacing: Theme.spacingXs
                            
                            Text {
                                text: modelData.title
                                color: Theme.textSecondary
                                font.pixelSize: Theme.fontSm
                            }
                            Text {
                                text: modelData.value
                                color: Theme.textPrimary
                                font.pixelSize: Theme.fontXl
                                font.bold: true
                            }
                            Text {
                                text: modelData.change
                                color: modelData.positive ? Theme.success : Theme.error
                                font.pixelSize: Theme.fontSm
                            }
                        }
                    }
                }
            }
            
            // Chart placeholder
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 300
                color: Theme.bgCard
                radius: Theme.radiusMd
                
                Text {
                    anchors.centerIn: parent
                    text: "Chart Area"
                    color: Theme.textMuted
                }
            }
        }
    }
}
```

### Alternative: Drawer-Based Navigation

For a collapsible sidebar or mobile-responsive design:

```qml
Drawer {
    id: sidebar
    width: 250
    height: parent.height
    
    // Navigation items...
}

// Hamburger button in header
Button {
    icon.source: "menu.svg"
    onClicked: sidebar.open()
}
```

### Navigation Controls Reference

| Control | Use Case |
|---------|----------|
| `StackLayout` | Switch between pages (sidebar nav) |
| `StackView` | Push/pop navigation (drill-down) |
| `SwipeView` | Horizontal page swiping |
| `TabBar` | Bottom/top tabs |
| `Drawer` | Slide-out panel |

---

## 11. System Tray Integration

### Important: System Tray Requires QtWidgets

QML alone doesn't have system tray support. You need `QSystemTrayIcon` from QtWidgets, even in a QML app.

### Complete Example

```python
import sys
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction
from PySide6.QtQml import QQmlApplicationEngine

# Use QApplication (not QGuiApplication) for system tray support
app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)  # Keep running when window closes

# Create system tray
tray = QSystemTrayIcon()
tray.setIcon(QIcon(":/icons/app_icon.png"))
tray.setToolTip("XPST - Cross-Post")

# Create tray menu
menu = QMenu()

show_action = QAction("Show XPST")
show_action.triggered.connect(lambda: show_window())
menu.addAction(show_action)

menu.addSeparator()

post_action = QAction("Quick Post...")
post_action.triggered.connect(lambda: quick_post())
menu.addAction(post_action)

menu.addSeparator()

quit_action = QAction("Quit")
quit_action.triggered.connect(app.quit)
menu.addAction(quit_action)

tray.setContextMenu(menu)
tray.show()

# Handle tray icon click
tray.activated.connect(lambda reason: show_window() 
                       if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

# Load QML
engine = QQmlApplicationEngine()
engine.load('qml/main.qml')

# Helper functions
def show_window():
    for obj in engine.rootObjects():
        obj.show()
        obj.raise_()
        obj.requestActivate()

def quick_post():
    for obj in engine.rootObjects():
        obj.show()
        # Navigate to compose page
        obj.setProperty('currentPage', 1)

sys.exit(app.exec())
```

### Key Notes

- Use `QApplication` instead of `QGuiApplication` when using `QSystemTrayIcon`
- `app.setQuitOnLastWindowClosed(False)` keeps the app running when the window is hidden
- System tray works on Windows (system tray), macOS (menu bar), and Linux (notification area)
- Icon format: `.png` or `.ico` (16x16 or 32x32 recommended)

### QML Alternative: Qt.labs.platform (Limited)

```qml
import Qt.labs.platform 1.1

SystemTrayIcon {
    visible: true
    icon.source: "qrc:/icons/app.png"
    tooltip: "XPST"
    
    menu: Menu {
        MenuItem {
            text: "Show"
            onTriggered: window.show()
        }
        MenuSeparator {}
        MenuItem {
            text: "Quit"
            onTriggered: Qt.quit()
        }
    }
    
    onActivated: {
        if (reason === SystemTrayIcon.Trigger) {
            window.show()
        }
    }
}
```

**Note:** `Qt.labs.platform` is experimental. The Python `QSystemTrayIcon` approach is more reliable.

---

## 12. Native File Dialogs

### Option A: QML FileDialog (Qt Quick Dialogs)

```qml
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Dialogs

ApplicationWindow {
    // File picker for media
    FileDialog {
        id: mediaPicker
        title: "Select Media File"
        nameFilters: [
            "Images (*.png *.jpg *.jpeg *.gif *.webp)",
            "Videos (*.mp4 *.mov *.avi *.webm)",
            "All files (*)"
        ]
        fileMode: FileDialog.OpenFile
        currentFolder: StandardPaths.standardLocations(StandardPaths.PicturesLocation)[0]
        
        onAccepted: {
            console.log("Selected:", selectedFile)
            // selectedFile is a URL: file:///path/to/file
        }
    }
    
    Button {
        text: "Add Media"
        onClicked: mediaPicker.open()
    }
    
    // Folder picker
    FolderDialog {
        id: folderPicker
        title: "Select Output Folder"
        
        onAccepted: {
            console.log("Folder:", selectedFolder)
        }
    }
    
    // Save dialog
    FileDialog {
        id: saveDialog
        title: "Save Export"
        fileMode: FileDialog.SaveFile
        nameFilters: ["CSV files (*.csv)", "JSON files (*.json)"]
        defaultSuffix: "csv"
        
        onAccepted: {
            console.log("Save to:", selectedFile)
        }
    }
}
```

### Option B: Python QFileDialog (More Control)

```python
from PySide6.QtWidgets import QFileDialog, QApplication
from PySide6.QtCore import QObject, Slot, QUrl

class FileHelper(QObject):
    @Slot(result=str)
    def pickVideo(self):
        path, _ = QFileDialog.getOpenFileName(
            None,
            "Select Video",
            "",
            "Video Files (*.mp4 *.mov *.avi *.webm);;All Files (*)"
        )
        return path
    
    @Slot(result=list)
    def pickMultipleImages(self):
        paths, _ = QFileDialog.getOpenFileNames(
            None,
            "Select Images",
            "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;All Files (*)"
        )
        return paths
    
    @Slot(result=str)
    def pickFolder(self):
        path = QFileDialog.getExistingDirectory(
            None,
            "Select Folder"
        )
        return path
    
    @Slot(str, result=str)
    def saveFile(self, default_name):
        path, _ = QFileDialog.getSaveFileName(
            None,
            "Save File",
            default_name,
            "JSON Files (*.json);;All Files (*)"
        )
        return path
```

```qml
// QML usage
Button {
    text: "Select Video"
    onClicked: {
        let path = fileHelper.pickVideo()
        if (path) {
            videoPreview.source = "file:///" + path
        }
    }
}
```

### QML FileDialog Properties Reference

| Property | Description |
|----------|-------------|
| `fileMode` | `OpenFile`, `OpenFiles`, `SaveFile` |
| `nameFilters` | Array of filter strings |
| `defaultSuffix` | Auto-appended extension for SaveFile |
| `currentFolder` | Initial directory (URL) |
| `selectedFile` | Last selected file (URL) |
| `selectedFiles` | Array of selected files |
| `options` | `DontUseNativeDialog`, `DontConfirmOverwrite`, etc. |

### Platform Notes

- Native dialogs are used by default on Windows, macOS, Linux (GTK+), Android, iOS
- `DontUseNativeDialog` forces Qt's built-in dialog (for consistent cross-platform look)
- File paths in QML are URLs (`file:///path`); use `QUrl.toLocalFile()` to convert

---

## 13. Animations in QML

### Core Animation Types

| Type | Purpose |
|------|---------|
| `NumberAnimation` | Animate numeric values (x, y, width, opacity) |
| `ColorAnimation` | Animate color changes |
| `PropertyAnimation` | Generic property animation |
| `RotationAnimation` | Animate rotation |
| `SequentialAnimation` | Run animations one after another |
| `ParallelAnimation` | Run animations simultaneously |
| `Behavior` | Default animation for any property change |
| `Transition` | Animate between states |
| `SmoothedAnimation` | Smoothly track a target value |
| `SpringAnimation` | Spring-like motion |
| `PauseAnimation` | Insert delay |

### Basic Number Animation

```qml
Rectangle {
    id: card
    width: 200; height: 100
    color: Theme.bgCard
    
    NumberAnimation {
        id: slideIn
        target: card
        property: "x"
        from: -200
        to: 0
        duration: 300
        easing.type: Easing.OutCubic
    }
    
    Component.onCompleted: slideIn.start()
}
```

### Behavior (Auto-animate Property Changes)

```qml
Rectangle {
    id: button
    width: 120; height: 40
    color: hovered ? Theme.accentPurple : Theme.bgTertiary
    
    property bool hovered: hoverArea.containsMouse
    
    // ANY color change will animate automatically
    Behavior on color {
        ColorAnimation { duration: 200 }
    }
    
    // Smooth width changes
    Behavior on width {
        NumberAnimation { duration: 150; easing.type: Easing.OutCubic }
    }
    
    MouseArea {
        id: hoverArea
        anchors.fill: parent
        hoverEnabled: true
    }
}
```

### State Transitions

```qml
Rectangle {
    id: postCard
    width: 300; height: 100
    color: Theme.bgCard
    
    state: "idle"
    
    states: [
        State {
            name: "idle"
            PropertyChanges { target: postCard; scale: 1.0; opacity: 1.0 }
        },
        State {
            name: "sending"
            PropertyChanges { target: postCard; scale: 0.95; opacity: 0.7 }
        },
        State {
            name: "sent"
            PropertyChanges { target: postCard; scale: 1.0; opacity: 1.0; color: "#1a3d1a" }
        },
        State {
            name: "error"
            PropertyChanges { target: postCard; color: "#3d1a1a" }
        }
    ]
    
    transitions: [
        Transition {
            from: "*"; to: "*"
            ParallelAnimation {
                NumberAnimation { property: "scale"; duration: 200; easing.type: Easing.InOutQuad }
                NumberAnimation { property: "opacity"; duration: 200 }
                ColorAnimation { duration: 300 }
            }
        }
    ]
}
```

### Sequential & Parallel Animations

```qml
// Fade in then slide
SequentialAnimation {
    id: enterAnimation
    
    ParallelAnimation {
        NumberAnimation { target: item; property: "opacity"; from: 0; to: 1; duration: 200 }
        NumberAnimation { target: item; property: "y"; from: 20; to: 0; duration: 200; easing.type: Easing.OutCubic }
    }
    
    PauseAnimation { duration: 100 }  // Brief pause
    
    NumberAnimation { target: item; property: "scale"; from: 0.9; to: 1.0; duration: 150 }
}
```

### Page Transition Animation

```qml
StackLayout {
    id: stackLayout
    
    // Add transition effect when changing pages
    onCurrentIndexChanged: {
        // Could trigger enter animation on the new page
    }
}

// Or use StackView for built-in transitions
StackView {
    id: stackView
    initialItem: dashboardPage
    
    pushEnter: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 200 }
            NumberAnimation { property: "x"; from: 50; to: 0; duration: 200; easing.type: Easing.OutCubic }
        }
    }
    
    popExit: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 200 }
            NumberAnimation { property: "x"; from: 0; to: 50; duration: 200 }
        }
    }
}
```

### Easing Curves

| Type | Description |
|------|-------------|
| `Easing.Linear` | Constant speed |
| `Easing.InQuad` | Slow start, fast end |
| `Easing.OutQuad` | Fast start, slow end |
| `Easing.InOutQuad` | Slow start and end |
| `Easing.OutCubic` | Smooth deceleration (great for UI) |
| `Easing.OutBack` | Slight overshoot |
| `Easing.OutElastic` | Bouncy |
| `Easing.OutBounce` | Bouncing ball effect |

### Loading Spinner Animation

```qml
Rectangle {
    id: spinner
    width: 40; height: 40
    radius: 20
    color: "transparent"
    border.color: Theme.accentPurple
    border.width: 3
    
    // Only show top arc
    Rectangle {
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        width: 10; height: 10
        radius: 5
        color: Theme.accentPurple
    }
    
    RotationAnimation {
        target: spinner
        property: "rotation"
        from: 0; to: 360
        duration: 1000
        loops: Animation.Infinite
        running: spinner.visible
    }
}
```

### Animated Visibility

```qml
Rectangle {
    id: notification
    width: 300; height: 60
    color: Theme.success
    visible: false
    opacity: 0
    
    function show(message) {
        notificationText.text = message
        visible = true
        showAnim.start()
        hideTimer.start()
    }
    
    ParallelAnimation {
        id: showAnim
        NumberAnimation { target: notification; property: "opacity"; to: 1; duration: 200 }
        NumberAnimation { target: notification; property: "y"; from: -60; to: 0; duration: 200; easing.type: Easing.OutCubic }
    }
    
    Timer {
        id: hideTimer
        interval: 3000
        onTriggered: hideAnim.start()
    }
    
    SequentialAnimation {
        id: hideAnim
        NumberAnimation { target: notification; property: "opacity"; to: 0; duration: 200 }
        PropertyAction { target: notification; property: "visible"; value: false }
    }
}
```

---

## 14. Qt Resource System (.qrc)

### Why Use It

- Embeds assets (icons, images, fonts, QML files) directly into the Python application
- Eliminates path issues when distributing/packaging
- Cross-platform guaranteed paths

### Step 1: Create a .qrc File

```xml
<!-- resources.qrc -->
<!DOCTYPE RCC>
<RCC version="1.0">
    <qresource prefix="/qml">
        <file alias="main.qml">qml/main.qml</file>
        <file alias="Theme.qml">qml/Theme.qml</file>
    </qresource>
    <qresource prefix="/icons">
        <file alias="app.svg">assets/icons/app.svg</file>
        <file alias="youtube.svg">assets/icons/youtube.svg</file>
        <file alias="instagram.svg">assets/icons/instagram.svg</file>
        <file alias="twitter.svg">assets/icons/twitter.svg</file>
        <file alias="tiktok.svg">assets/icons/tiktok.svg</file>
    </qresource>
    <qresource prefix="/images">
        <file alias="logo.png">assets/images/logo.png</file>
        <file alias="placeholder.png">assets/images/placeholder.png</file>
    </qresource>
</RCC>
```

### Step 2: Compile Resources

```bash
pyside6-rcc resources.qrc -o resources_rc.py
```

### Step 3: Import in Python

```python
# main.py
import resources_rc  # Must import to register resources!

import sys
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

app = QGuiApplication(sys.argv)
engine = QQmlApplicationEngine()

# Load QML from resource path
engine.load(':/qml/main.qml')

sys.exit(app.exec())
```

### Step 4: Use in QML

```qml
Image {
    source: "qrc:/icons/youtube.svg"
}

Image {
    source: "qrc:/images/logo.png"
}

// Or using the :/ prefix
Image {
    source: ":/icons/app.svg"
}
```

### Step 5: Use in Python

```python
from PySide6.QtGui import QIcon

icon = QIcon(":/icons/app.svg")
```

### Resource Path Format

```
:/prefix/alias
```
- `:` — indicates a resource path
- `/prefix` — the `<qresource prefix="...">` value
- `/alias` — the `<file alias="...">` value

### Build Script Integration

```python
# build_resources.py
import subprocess
import os

def build_resources():
    """Compile .qrc to Python"""
    subprocess.run([
        'pyside6-rcc',
        'resources.qrc',
        '-o', 'resources_rc.py'
    ], check=True)
    print("Resources compiled successfully")

if __name__ == '__main__':
    build_resources()
```

### Tips

- **SVG icons** are recommended — resolution-independent, small file size
- **Font-based icons** (Font Awesome, Material Icons) are even better for scalability
- **Always import `resources_rc`** before loading QML that references resources
- **Recompile** when adding/removing/updating assets
- Use `pyside6-rcc --list` to verify included resources

---

## 15. Packaging PySide6 Apps

### Option A: PyInstaller (Most Common)

```bash
pip install pyinstaller
```

#### Basic Build

```bash
# Simple build
pyinstaller main.py

# Recommended flags for GUI app
pyinstaller --windowed --name "XPST" main.py
```

#### Advanced Build (with resources)

```bash
pyinstaller \
    --windowed \
    --name "XPST" \
    --icon assets/icons/app.ico \
    --add-data "qml/:qml" \
    --add-data "assets/:assets" \
    --add-data "resources_rc.py:." \
    --hidden-import resources_rc \
    main.py
```

#### Using .spec File (Recommended for Repeatable Builds)

```python
# XPST.spec
# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('qml/', 'qml'),
        ('assets/', 'assets'),
    ],
    hiddenimports=['resources_rc'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='XPST',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window
    icon='assets/icons/app.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='XPST',
)
```

Build with spec file:
```bash
pyinstaller XPST.spec
```

#### Handling Resource Paths in Packaged App

```python
import sys
import os

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    if hasattr(sys, '_MEIPASS'):
        # Running as PyInstaller bundle
        return os.path.join(sys._MEIPASS, relative_path)
    # Running as normal Python script
    return os.path.join(os.path.abspath('.'), relative_path)

# Usage
icon_path = resource_path('assets/icons/app.ico')
qml_path = resource_path('qml/main.qml')
```

#### Platform-Specific Notes

**Windows:**
```bash
pyinstaller --windowed --onefile --icon=app.ico main.py
```
- Use `--onefile` for single executable (slower startup)
- Use `--icon` for the exe icon
- Set Windows app ID for proper taskbar grouping:
```python
try:
    from ctypes import windll
    windll.shell32.SetCurrentProcessExplicitAppUserModelID('xpst.app.1.0')
except ImportError:
    pass
```

**macOS:**
```bash
pyinstaller --windowed --onefile main.py
```
- Creates `.app` bundle in `dist/`
- For DMG: use `create-dmg` or `dmgbuild`

**Linux:**
```bash
pyinstaller --onefile main.py
```
- Consider AppImage format for distribution

### Option B: cx_Freeze

```bash
pip install cx_Freeze
```

```python
# setup.py
from cx_Freeze import setup, Executable

build_exe_options = {
    "packages": ["PySide6"],
    "include_files": [
        ("qml/", "qml"),
        ("assets/", "assets"),
    ],
}

setup(
    name="XPST",
    version="1.0.0",
    description="Social Media Cross-Post Tool",
    options={"build_exe": build_exe_options},
    executables=[Executable("main.py", base="Win32GUI", icon="app.ico")],
)
```

Build:
```bash
python setup.py build
```

### Option C: Briefcase (BeeWare)

```bash
pip install briefcase
briefcase new  # Create new project
briefcase build  # Build for current platform
briefcase run    # Run the built app
```

Briefcase creates native app bundles:
- `.app` on macOS
- `.msi` on Windows
- `.AppImage` on Linux

### Recommendation for XPST

**Use PyInstaller** — it's the most widely used, best documented, and has the most community support for PySide6 apps. Use the `.spec` file approach for reproducible builds.

---

## 16. Best Practices Summary

### Architecture

1. **Separate UI from logic**: All business logic in Python, all UI in QML
2. **Use context properties or QmlElement** to expose Python to QML
3. **Store state in models**, not in delegates (delegates are recycled)
4. **Use explicit types** for QML properties — avoid `var` when possible

### Theming

5. **Use a Theme.qml singleton** as single source of truth for all design tokens
6. **Material Design style** provides built-in dark/light themes and Material colors
7. **Dense variant** for desktop apps (smaller controls for mouse interaction)

### Performance

8. **Use Qt Quick Layouts** for responsive resizing
9. **Prefer declarative bindings** over imperative `Component.onCompleted` assignments
10. **Use SVG icons** or font-based icons for resolution independence
11. **Provide @2x, @3x** resources for high-DPI displays

### Resources

12. **Use .qrc resource system** for all bundled assets
13. **Always compile resources** before building
14. **Import `resources_rc`** at module level in Python

### Development

15. **Use `pyside6-qml`** for quick QML prototyping
16. **Use Qt Design Studio** or Qt Creator for visual QML editing
17. **Follow QML coding conventions** (Qt official)

---

## 17. XPST Architecture Recommendation

### Recommended Stack

```
Framework:    PySide6 with QML (Qt Quick)
UI Style:     Material Design (Dark theme) + custom Theme.qml
Charts:       QtCharts (native QML charts)
Navigation:   Sidebar + StackLayout
Backend:      Python QObject classes exposed via setContextProperty
Models:       QAbstractListModel for dynamic lists
Resources:    Qt Resource System (.qrc)
System Tray:  QSystemTrayIcon (QtWidgets)
File Dialogs: QML FileDialog (native look)
Packaging:    PyInstaller with .spec file
```

### Entry Point

```python
# main.py
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtQml import QQmlApplicationEngine

import resources_rc  # Compiled resources

from backend.controller import AppController
from backend.post_manager import PostManager
from backend.models import PostListModel

def main():
    # Use QApplication (not QGuiApplication) for system tray
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("XPST")
    app.setOrganizationName("XPST")
    
    engine = QQmlApplicationEngine()
    
    # Create backend
    controller = AppController()
    post_manager = PostManager()
    post_model = PostListModel()
    
    # Expose to QML
    ctx = engine.rootContext()
    ctx.setContextProperty("controller", controller)
    ctx.setContextProperty("postManager", post_manager)
    ctx.setContextProperty("postModel", post_model)
    
    # Load UI
    engine.load(':/qml/main.qml')
    
    if not engine.rootObjects():
        print("Error: Failed to load QML")
        sys.exit(-1)
    
    engine.quit.connect(app.quit)
    
    # System tray setup
    setup_system_tray(app, engine)
    
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
```

### Key Imports Cheat Sheet

```python
# Python side
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QFileDialog
from PySide6.QtGui import QGuiApplication, QIcon, QAction
from PySide6.QtQml import QQmlApplicationEngine, QmlElement
from PySide6.QtCore import QObject, Signal, Slot, Property, QTimer, Qt, QAbstractListModel, QModelIndex, QPointF, QUrl
```

```qml
// QML side
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Controls.Material 2.15
import QtQuick.Layouts 1.15
import QtQuick.Dialogs
import QtCharts 2.15
```

---

## Sources & References

- [Qt for Python Documentation](https://doc.qt.io/qtforpython-6/)
- [Python GUIs - PySide6 Tutorial](https://www.pythonguis.com/pyside6-tutorial/)
- [Qt Quick Best Practices](https://doc.qt.io/qtforpython-6/overviews/qtquick-bestpractices.html)
- [Material Style Documentation](https://doc.qt.io/qtforpython-6/overviews/qtquickcontrols-material.html)
- [PySide6 QML Signal Examples](https://github.com/fernicar/pyside6_examples_doc_2025_v6.9.1)
- [QtCharts in PySide/QML](https://www.dmcinfo.com/blog/17537/using-qtcharts-in-a-pyside-qml-application/)
- [QAbstractListModel in QML](https://www.dmcinfo.com/blog/17671/using-a-qabstractlistmodel-in-qml/)
- [Three Dashboards Comparison](https://www.learnqt.guide/three-dashboards-one-weekend)
- [qt-material Library](https://qt-material.readthedocs.io/)
- [PySide6 QResource System](https://www.pythonguis.com/tutorials/pyside6-qresource-system/)
- [Packaging PySide6 Apps](https://www.pythonguis.com/tutorials/packaging-pyside6-applications-windows-pyinstaller-installforge/)
- [PySide6 vs PyQt6 Comparison](https://www.pythonguis.com/faq/pyqt6-vs-pyside6/)
- [System Tray with PySide6](https://www.pythonguis.com/tutorials/pyside6-system-tray-mac-menu-bar-applications/)
- [QML FileDialog Documentation](https://doc.qt.io/qt-6/qml-qtquick-dialogs-filedialog.html)
- [Qt Navigation Controls](https://doc.qt.io/qt-6/qtquickcontrols-navigation.html)

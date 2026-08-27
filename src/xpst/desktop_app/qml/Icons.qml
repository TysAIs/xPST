pragma Singleton
import QtQuick 2.15

// Central icon mapping (W4-5). Single source of truth at the QML layer for the
// bundled Lucide icon font: it loads the font and exposes the family name plus
// logical-name -> glyph values. The same codepoints are defined in the Qt-free
// Python module icon_glyphs.py and mirrored onto the live ThemeProvider, so
// existing `theme.icon*` references resolve to these glyphs.
//
// Usage in QML:
//     Text { font.family: Icons.family; text: Icons.youtube }
// or via the live theme provider:
//     Text { font.family: theme.iconFontFamily; text: theme.iconYouTube }
QtObject {
    id: icons

    // Load the bundled font. The desktop app injects `iconFontUrl` (an
    // absolute URL resolved frozen-aware via resource_path in main.py):
    // in a frozen .app bundle the data side lives under Contents/Resources,
    // so a fixed relative depth from this file cannot reach it in every
    // layout and previously logged a FontLoader error on every launch. The
    // document-relative fallback covers standalone QML tooling run from a
    // source tree, where assets/ is four levels up (repo root).
    property FontLoader _loader: FontLoader {
        source: iconFontUrl ? Qt.resolvedUrl(iconFontUrl)
                            : Qt.resolvedUrl("../../../../assets/fonts/lucide.ttf")
    }

    // The family name the bundled TTF registers as. Falls back to the loader's
    // reported name once the font finishes loading.
    readonly property string family: "lucide"

    // ── Glyphs (codepoints verified present in the bundled font cmap) ──
    // Platform icons (neutral; the platform colour carries brand identity).
    readonly property string youtube: ""    // monitor-play
    readonly property string instagram: ""  // camera
    readonly property string x: ""           // hash
    readonly property string tiktok: ""      // music

    // Sidebar chrome
    readonly property string logo: ""        // zap
    readonly property string bell: ""        // bell
    readonly property string moon: ""        // moon
    readonly property string sun: ""         // sun
    readonly property string stats: ""       // chart-column

    // Navigation
    readonly property string dashboard: ""   // layout-dashboard
    readonly property string content: ""     // layout-grid
    readonly property string analytics: ""   // chart-line
    readonly property string connect: ""     // plug
    readonly property string schedule: ""    // clock
    readonly property string settings: ""    // settings
    readonly property string about: ""       // info

    // Status / actions
    readonly property string check: ""       // check
    readonly property string error: ""       // circle-alert
    readonly property string warning: ""     // triangle-alert
    readonly property string close: ""       // x
    readonly property string edit: ""        // pencil
    readonly property string web: ""         // globe
    readonly property string users: ""       // users
    readonly property string trophy: ""      // trophy
    readonly property string calendar: ""    // calendar
    readonly property string video: ""       // video
    readonly property string trash: ""       // trash-2
    readonly property string retry: ""       // rotate-cw
    readonly property string plus: ""        // plus
    readonly property string search: ""      // search

    // Analytics metrics
    readonly property string eye: ""         // eye (views)
    readonly property string heart: ""       // heart (likes)
    readonly property string message: ""     // message-circle (comments)
    readonly property string share: ""       // share-2 (shares)
    readonly property string trending: ""    // trending-up (comparison delta)

    // About / links
    readonly property string home: ""        // home (homepage)
    readonly property string code: ""        // code (repository)
    readonly property string book: ""        // book-open (documentation)
    readonly property string bug: ""         // bug (report issues)
    readonly property string filetext: ""    // file-text (changelog)
    readonly property string external: ""    // external-link (open link arrow)
}

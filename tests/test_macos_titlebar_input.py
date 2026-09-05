from pathlib import Path

QML = Path(__file__).parents[1] / "src/xpst/desktop_app/qml/main.qml"


def test_macos_titlebar_drag_does_not_cover_navigation_surface():
    text = QML.read_text(encoding="utf-8")
    block = text[text.index("id: macTitleBarDrag") :]
    block = block[: block.index("\n    }", block.index("onPressed")) + 6]
    assert "anchors.leftMargin: 80" in block
    assert "anchors.fill: parent" not in block
    assert "mouse.x < 80" not in block

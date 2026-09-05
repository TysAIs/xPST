from pathlib import Path

QML = Path(__file__).parents[1] / "src/xpst/desktop_app/qml/main.qml"


def test_window_restore_clamps_persisted_geometry_to_connected_screen():
    text = QML.read_text(encoding="utf-8")
    assert "target.virtualX + target.width - w" in text
    assert "Math.max(minX, Math.min(x, maxX))" in text
    assert "coordinates can refer to a removed monitor" in text
    # The old implementation trusted a matching screen name and assigned
    # negative coordinates directly; keep that regression from returning.
    assert "root.x = x\n                            root.y = y" not in text

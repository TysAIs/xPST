"""Windows atomic media replacement regression."""

from pathlib import Path

import pytest

import xpst.utils.video as video_module
from xpst.utils.video import VideoProcessor


def test_encode_retries_destination_replace_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = VideoProcessor.__new__(VideoProcessor)
    output = tmp_path / "encoded.mp4"
    temp = tmp_path / "encoded.mp4.unique.tmp.mp4"
    temp.write_bytes(b"encoded" * 400)
    attempts = {"count": 0}

    original_replace = video_module.os.replace

    def replace(source: Path, destination: Path) -> None:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise PermissionError("destination is temporarily locked")
        original_replace(source, destination)

    monkeypatch.setattr(video_module.os, "replace", replace)

    # Exercise the bounded retry helper introduced around the Windows replace.
    processor._replace_encoded_output(temp, output)

    assert attempts["count"] == 2
    assert output.read_bytes() == b"encoded" * 400

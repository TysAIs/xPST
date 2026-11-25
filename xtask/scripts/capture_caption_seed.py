"""Capture `xpst suggest-caption`'s seed derivation from the shipped Python.

`src/xpst/cli.py::suggest_caption` builds its caption seed as

    Path(video_path).stem.replace("_", " ").replace("-", " ")

when the KB transcriber is unavailable. `Path.stem` is NOT `Path::file_stem`:
Python's `_splitext` keeps a trailing dot (`clip.` -> `clip.`), keeps a
leading-dot name whole (`.mp4` -> `.mp4`), and `PurePath.name` answers `''` for
`.` and `/` but `..` for `..`. The Windows flavour is a different parser again
(`PureWindowsPath` treats `\\` as a separator and splits a drive off the first
component), so both flavours are captured here and replayed by
`crates/xpst-surface/tests/caption_seed.rs` — the Windows rows are pinned on
Linux instead of waiting for a runner.

Run from the repository root:

    PYTHONPATH=src python xtask/scripts/capture_caption_seed.py

Re-running must reproduce the committed fixture byte for byte.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "crates/xpst-surface/tests/fixtures/python_caption_seed.json"

# Every shape a `click.Path(exists=True)` argument can carry, plus the ones a
# hand-port gets wrong: a trailing dot, a leading-dot name, a doubled
# separator, a `.` component, `..`, a root, a trailing space after the
# extension, a multi-dot name, and non-ASCII.
CASES = [
    "clip.mp4",
    "my_clip-final.mp4",
    "a.b.mp4",
    "clip",
    "clip.",
    ".mp4",
    ".hidden",
    "dir/clip.mp4",
    "dir/clip.mp4/",
    "dir/",
    ".",
    "..",
    "/",
    "//",
    "",
    "a/./b.mp4",
    "a//b.mp4",
    "a/.",
    "a/..",
    "./clip.mp4",
    "clip.MP4",
    "clip.tar.gz",
    "  spaced name .mp4",
    "clip.mp4 ",
    "üñï_clip.mp4",
    "dir/.hidden.mp4",
    "a/b/c",
    "clip..mp4",
    "-leading-dash.mp4",
    "under_score-and-dash.mp4",
    "no_extension",
    "trailing_underscore_",
    "a/b/../c.mp4",
]

# Windows-only shapes: a drive-absolute path, a drive-relative path, a UNC
# path, a backslash separator, and a mixed pair.
WINDOWS_CASES = [
    "C:\\clips\\my_clip.mp4",
    "C:/clips/my_clip.mp4",
    "C:clip.mp4",
    "C:",
    "\\\\server\\share\\clip.mp4",
    "clips\\clip.mp4",
    "clips\\clip.mp4\\",
    "C:\\clips\\a.b.mp4",
    "C:\\clips\\clip.",
    "C:\\clips\\.mp4",
]


def seed(path: str, flavour: str) -> str:
    """The exact expression `cli.py::suggest_caption` runs."""
    pure = PureWindowsPath(path) if flavour == "windows" else PurePosixPath(path)
    return pure.stem.replace("_", " ").replace("-", " ")


def main() -> int:
    rows = []
    for flavour, cases in (("posix", CASES), ("windows", WINDOWS_CASES)):
        for path in cases:
            pure = PureWindowsPath(path) if flavour == "windows" else PurePosixPath(path)
            rows.append(
                {
                    "path": path,
                    "flavour": flavour,
                    "name": pure.name,
                    "stem": pure.stem,
                    "seed": seed(path, flavour),
                }
            )
    if not rows:
        raise SystemExit("refusing to write an empty fixture")
    document = {
        "schema_version": 1,
        "source": "src/xpst/cli.py::suggest_caption",
        "expression": 'Path(video_path).stem.replace("_", " ").replace("-", " ")',
        "note": (
            "Captured by importing pathlib and evaluating the shipped expression. "
            "`flavour` selects PurePosixPath or PureWindowsPath, so the Windows "
            "rows are pinned on any host."
        ),
        "row_count": len(rows),
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

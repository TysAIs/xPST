"""Every adapter must bound stored errors word-safely.

PR #277 added ``truncate_error()`` and wired it into ``x.py`` and
``upload_service.py`` only. The other adapters still sliced error text at a
fixed width, so new DLQ entries were still cut mid-word — one stored TikTok
error ended ``or run: xpst auth ti``. This is the source-level guard that keeps
the promise true for every platform module, not one.
"""

import re
from pathlib import Path

PLATFORMS_DIR = Path(__file__).resolve().parents[1] / "src" / "xpst" / "platforms"

# A fixed-width slice of an error body, e.g. ``str(e)[:200]`` or ``resp.text[:300]``.
RAW_ERROR_SLICE = re.compile(r"\[:(?:200|300)\]")


def _adapters() -> list[str]:
    return sorted(
        path.stem
        for path in PLATFORMS_DIR.glob("*.py")
        if path.stem not in {"__init__", "base", "twikit_compat"}
    )


def test_no_adapter_slices_an_error_at_a_fixed_width() -> None:
    offenders: dict[str, list[str]] = {}
    for name in _adapters():
        source = (PLATFORMS_DIR / f"{name}.py").read_text(encoding="utf-8")
        hits = [
            line.strip()
            for line in source.splitlines()
            if RAW_ERROR_SLICE.search(line)
        ]
        if hits:
            offenders[name] = hits

    assert not offenders, f"raw error slices remain (use truncate_error): {offenders}"


def test_every_adapter_imports_the_word_safe_helper() -> None:
    missing = [
        name
        for name in _adapters()
        if "truncate_error"
        not in (PLATFORMS_DIR / f"{name}.py").read_text(encoding="utf-8")
    ]

    assert not missing, f"adapters with no word-safe error helper: {missing}"

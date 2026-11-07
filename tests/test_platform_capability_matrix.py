"""The platform capability matrix must not drift from the engine it describes.

``docs/PLATFORM-CAPABILITY-MATRIX.md`` is the document the product uses to decide
what it may promise about a destination. Its §3 table is *generated* from the
canonical capability document, so a destination whose shipped content types or
media transport change — or a destination added to the engine — has to update the
matrix in the same change.

The failure this prevents is the one the matrix exists for: a product claiming a
platform it cannot publish to, or a platform it can publish to in a form the
matrix does not mention. It also enforces that no shipped publishing destination
lacks a vendor-access row (§2), because a destination without a documented gate
is a destination nobody can explain to a user.
"""

from __future__ import annotations

import re
from pathlib import Path

from xpst.content import capability_document

REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX_MD = REPO_ROOT / "docs" / "PLATFORM-CAPABILITY-MATRIX.md"

#: How the matrix spells a destination's media transport. The engine uses enum
#: values; the document is for humans. Keep the mapping exhaustive so a new
#: transport value fails here instead of being silently ignored.
TRANSPORT_LABELS = {
    "local_upload": "local upload",
    "public_url": "public URL",
}

#: Display names the matrix may use for a destination, when the engine's
#: ``display_name`` is marketing-flavoured ("YouTube Shorts", "Instagram Reels").
#: A destination missing from both this map and the document's ``display_name``
#: fails the test, which is the point: the matrix needs a row, not a guess.
DISPLAY_ALIASES = {
    "youtube": {"youtube", "youtube shorts"},
    "x": {"x"},
    "instagram": {"instagram", "instagram reels"},
    "threads": {"threads"},
    "facebook": {"facebook", "facebook page"},
    "tiktok": {"tiktok"},
    "messenger": {"messenger"},
}


def _matrix_text() -> str:
    return MATRIX_MD.read_text(encoding="utf-8")


def _section(text: str, heading_prefix: str) -> str:
    """Return the body of the top-level section whose heading starts with ``heading_prefix``."""
    pattern = re.compile(
        r"^## " + re.escape(heading_prefix) + r".*?$(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    assert match is not None, f"no '## {heading_prefix}' section in {MATRIX_MD.name}"
    return match.group(1)


def _table_rows(body: str) -> list[list[str]]:
    """Return data rows of the first markdown table in ``body`` as cell lists."""
    rows: list[list[str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue  # separator row
        rows.append(cells)
    return rows[1:] if rows else []  # drop the header row


def _destination_rows() -> dict[str, list[str]]:
    """Map a matrix destination label (lowercased) to its §3 row cells."""
    body = _section(_matrix_text(), "3. What xPST ships today")
    rows = _table_rows(body)
    assert rows, "the shipped-destinations table is empty"
    return {row[0].lower(): row for row in rows if len(row) >= 3}


def _vendor_rows() -> list[str]:
    body = _section(_matrix_text(), "2. Vendor access classification")
    return [row[0] for row in _table_rows(body) if row]


def _shipped_destinations() -> dict[str, dict]:
    document = capability_document()
    return {key: profile for key, profile in document["platforms"].items()}


def _label_for(platform: str) -> str:
    profile = _shipped_destinations()[platform]
    display = str(profile["display_name"]).lower()
    rows = _destination_rows()
    for candidate in (display, platform.lower(), *DISPLAY_ALIASES.get(platform, set())):
        if candidate in rows:
            return candidate
    raise AssertionError(
        f"docs/PLATFORM-CAPABILITY-MATRIX.md §3 has no row for the shipped destination "
        f"{platform!r} (looked for {display!r}, {platform!r}). Add the row and its "
        f"implemented content types or the matrix is claiming a platform it cannot describe."
    )


def _plain(cell: str) -> str:
    """Strip markdown emphasis/code markers from a table cell before comparing."""
    return cell.replace("**", "").replace("`", "").strip()


def _parse_content_types(cell: str) -> list[str]:
    cell = _plain(cell)
    if not cell or cell == "—":
        return []
    types = []
    for item in re.split(r",|\band\b", cell):
        token = _plain(item)
        if token:
            types.append(token)
    return sorted(types)


def test_every_publishing_destination_has_a_matrix_row_with_its_shipped_types() -> None:
    for platform, profile in _shipped_destinations().items():
        if not profile.get("publishing"):
            continue
        row = _destination_rows()[_label_for(platform)]
        assert _parse_content_types(row[1]) == sorted(profile["implemented"]), (
            f"{platform}: matrix §3 says {_parse_content_types(row[1])}, engine implements "
            f"{sorted(profile['implemented'])}"
        )


def test_matrix_transport_matches_the_engine() -> None:
    for platform, profile in _shipped_destinations().items():
        if not profile.get("publishing"):
            continue
        transport = profile.get("media_transport")
        assert transport in TRANSPORT_LABELS, (
            f"{platform}: unknown media_transport {transport!r}; add it to "
            f"TRANSPORT_LABELS and to the matrix rather than letting the routing preview guess"
        )
        row = _destination_rows()[_label_for(platform)]
        assert TRANSPORT_LABELS[transport].lower() in _plain(row[2]).lower(), (
            f"{platform}: matrix §3 transport cell {row[2]!r} does not state "
            f"{TRANSPORT_LABELS[transport]!r} (engine says {transport})"
        )


def test_matrix_lists_no_destination_the_engine_does_not_have() -> None:
    shipped = _shipped_destinations()
    known = {str(profile["display_name"]).lower() for profile in shipped.values()}
    known |= set(shipped)
    for platform, labels in DISPLAY_ALIASES.items():
        if platform in shipped:
            known |= labels
    for label in _destination_rows():
        assert label in known, (
            f"matrix §3 lists {label!r}, which the engine's capability document does not "
            f"know. Either remove the row or add the destination."
        )


def test_every_publishing_destination_has_a_vendor_access_row() -> None:
    """A shipped destination without a vendor classification has no explainable gate."""
    vendor_labels = {label.lower() for label in _vendor_rows()}
    for platform, profile in _shipped_destinations().items():
        if not profile.get("publishing"):
            continue
        names = {str(profile["display_name"]).lower(), platform.lower()}
        names |= DISPLAY_ALIASES.get(platform, set())
        assert any(
            any(label.startswith(name) or name.startswith(label) for name in names)
            for label in vendor_labels
        ), (
            f"{platform}: no row in matrix §2 (vendor access classification). A destination "
            f"whose gate is not documented cannot be explained to a user."
        )

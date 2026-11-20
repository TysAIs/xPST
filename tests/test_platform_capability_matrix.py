"""The platform capability matrix must not drift from the engine it describes.

``docs/PLATFORM-CAPABILITY-MATRIX.md`` is the document the product uses to decide
what it may promise about a destination. Its §3 destination table is *generated*
from the canonical capability document, so a destination whose shipped content
types or media transport change — or a destination added to the engine — has to
update the matrix in the same change.

The failure this prevents is the one the matrix exists for: a product claiming a
platform it cannot publish to, or a platform it can publish to in a form the
matrix does not mention. It also enforces that no shipped publishing destination
lacks a vendor-access row (§2), because a destination without a documented gate
is a destination nobody can explain to a user.

The same rules now cover the stream pillar. §3 carries a second table for live
ingest, asserted against :mod:`xpst.stream.destinations`: every registered
ingest path needs a row with its class and its documented endpoint, every row
needs its vendor page in §8, no row may claim a live ingest this repository has
not actually performed, and the simulcast prohibitions are pinned as rows so
they cannot decay into prose.
"""

from __future__ import annotations

import re
from pathlib import Path

from xpst.content import capability_document
from xpst.stream.destinations import (
    SIMULCAST_REFUSALS,
    STREAM_INGEST_PATHS,
    UNSUPPORTED,
)

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

#: Platforms whose live ingest this repository has proved end to end: one stream
#: pushed through the relay and watched live *on that platform*.
#:
#: Empty is the honest value, and it is deliberately the starting point. The relay
#: proof that exists today is two MediaMTX destinations receiving the same bytes,
#: which proves the relay and not a single vendor's ingest. Adding a platform here
#: is the same commit as changing its matrix row, so no row can claim a proof
#: nobody performed.
PROVED_LIVE_INGEST: frozenset[str] = frozenset()


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


def _is_separator(row: list[str]) -> bool:
    return bool(row) and all(set(cell) <= {"-", ":"} and cell for cell in row)


def _tables(body: str) -> list[list[list[str]]]:
    """Every markdown table in ``body`` as a list of cell rows, header included.

    A section can hold more than one table (the shipped destinations, the live
    ingest paths, the refusals), so tables are split on their boundaries instead
    of being read as one run of ``|`` lines — the second table's header would
    otherwise be parsed as a destination named "platform".
    """
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if current:
                tables.append(current)
                current = []
            continue
        current.append([cell.strip() for cell in stripped.strip("|").split("|")])
    if current:
        tables.append(current)
    return tables


def _data_rows(table: list[list[str]]) -> list[list[str]]:
    return [row for row in table[1:] if not _is_separator(row)]


def _table_rows(body: str) -> list[list[str]]:
    """Return data rows of the first markdown table in ``body`` as cell lists."""
    tables = _tables(body)
    return _data_rows(tables[0]) if tables else []


def _destination_rows() -> dict[str, list[str]]:
    """Map a matrix destination label (lowercased) to its §3 row cells."""
    body = _section(_matrix_text(), "3. What xPST ships today")
    rows = _table_rows(body)
    assert rows, "the shipped-destinations table is empty"
    return {row[0].lower(): row for row in rows if len(row) >= 3}


def _vendor_rows() -> list[str]:
    body = _section(_matrix_text(), "2. Vendor access classification")
    return [row[0] for row in _table_rows(body) if row]


def _table_with_header(body: str, expected: tuple[str, ...]) -> list[list[str]]:
    """Return the data rows of the table whose first cells spell ``expected``."""
    for table in _tables(body):
        header = tuple(_plain(cell).lower() for cell in table[0][: len(expected)])
        if header == expected:
            return _data_rows(table)
    raise AssertionError(
        f"{MATRIX_MD.name} has no table headed {' | '.join(expected)!r}; the matrix has "
        f"lost a table the engine still describes"
    )


def _stream_rows() -> dict[str, list[str]]:
    """Map a §3 live-ingest label (lowercased) to its row cells."""
    body = _section(_matrix_text(), "3. What xPST ships today")
    rows = _table_with_header(body, ("platform", "ingest class"))
    assert rows, "the live-ingest table is empty"
    return {row[0].lower(): row for row in rows if len(row) >= 6}


def _stream_row_for(platform: str) -> list[str]:
    """Return the live-ingest row for a registry platform, refusal rows included."""
    path = STREAM_INGEST_PATHS[platform]
    rows = _stream_rows()
    if path.ingest_class != UNSUPPORTED:
        assert path.display_name.lower() in rows, (
            f"{platform}: §3's live-ingest table has no row for {path.display_name!r}"
        )
        return rows[path.display_name.lower()]
    for row in rows.values():
        if _plain(row[1]) == UNSUPPORTED and path.display_name.lower() in row[0].lower():
            return row
    raise AssertionError(
        f"{platform}: no §3 live-ingest row names {path.display_name!r} as unsupported. "
        "An unsupported platform must still be listed, or a surface will offer it."
    )


def _prohibition_rows() -> dict[str, list[str]]:
    body = _section(_matrix_text(), "3. What xPST ships today")
    rows = _table_with_header(body, ("refusal key", "prohibition"))
    return {_plain(row[0]).lower(): row for row in rows if len(row) >= 3}


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


def test_every_registered_stream_platform_has_a_matrix_row_with_its_class() -> None:
    """The shipped stream registry and the matrix must answer the ingest question alike."""
    rows = _stream_rows()
    for platform, path in STREAM_INGEST_PATHS.items():
        if path.ingest_class == UNSUPPORTED:
            # Unsupported platforms may share one refusal row, but each must be named.
            assert _stream_row_for(platform), platform
            continue
        assert path.display_name.lower() in rows, (
            f"{platform}: §3's live-ingest table has no row for {path.display_name!r}. The "
            f"engine registers an {path.ingest_class!r} ingest path that the matrix cannot "
            f"describe, so a surface cannot explain the gate."
        )
        assert _plain(rows[path.display_name.lower()][1]) == path.ingest_class, (
            f"{platform}: matrix says class {rows[path.display_name.lower()][1]!r}, the "
            f"registry says {path.ingest_class!r}"
        )


def test_matrix_lists_no_stream_platform_the_registry_does_not_have() -> None:
    known = {path.display_name.lower() for path in STREAM_INGEST_PATHS.values()}
    for label, row in _stream_rows().items():
        labels = (
            [token.strip() for token in label.split(",")]
            if _plain(row[1]) == UNSUPPORTED
            else [label]
        )
        for name in labels:
            assert name in known, (
                f"matrix §3's live-ingest table lists {name!r}, which "
                f"xpst.stream.destinations does not register. The registry is the source: "
                f"either add the platform there or remove the row."
            )


def test_stream_ingest_path_cell_matches_the_registry_endpoint() -> None:
    """A row that names a different endpoint than the code calls is a wrong instruction."""
    for platform, path in STREAM_INGEST_PATHS.items():
        if path.ingest_class == UNSUPPORTED:
            continue
        cell = _plain(_stream_row_for(platform)[2])
        if path.api_available:
            assert path.endpoint and path.endpoint in cell, (
                f"{platform}: matrix documents {cell!r}, the registry calls "
                f"{path.endpoint!r}"
            )
        else:
            assert cell.lower().startswith("none"), (
                f"{platform}: class is {path.ingest_class!r} but the matrix documents an "
                f"endpoint ({cell!r}); a per-session console has no endpoint to cite"
            )


def test_every_stream_platform_cites_its_vendor_page() -> None:
    """No gate claim without the page it came from, and no page that is just a site root."""
    references = _section(_matrix_text(), "8. References")
    for platform, path in STREAM_INGEST_PATHS.items():
        if not path.source_url:
            continue
        assert path.source_url in references, (
            f"{platform}: the registry's vendor page {path.source_url} is not cited in §8. "
            f"A gate with no dated citation is a guess."
        )


def test_every_relay_destination_has_a_vendor_access_row() -> None:
    vendor_labels = {_plain(label).lower() for label in _vendor_rows()}
    for platform, path in STREAM_INGEST_PATHS.items():
        if path.ingest_class == UNSUPPORTED:
            continue
        assert path.display_name.lower() in vendor_labels, (
            f"{platform}: §2 has no live-ingest row labelled {path.display_name!r}. A relay "
            f"destination whose vendor gate is undocumented cannot be explained to a user."
        )


def test_simulcast_prohibitions_are_rows_not_prose() -> None:
    rows = _prohibition_rows()
    assert set(rows) == {key.lower() for key in SIMULCAST_REFUSALS}, (
        f"matrix §3 refusal rows {sorted(rows)} do not match "
        f"{sorted(SIMULCAST_REFUSALS)} in xpst.stream.destinations"
    )
    for key, prohibition in SIMULCAST_REFUSALS.items():
        assert prohibition in _plain(rows[key.lower()][1]), (
            f"{key}: the matrix does not quote the registry's prohibition verbatim "
            f"({prohibition!r})"
        )


def test_no_stream_row_claims_a_live_proof_the_repository_has_not_made() -> None:
    """A platform is not a supported live destination until one ingest is proved."""
    for platform in STREAM_INGEST_PATHS:
        cell = _plain(_stream_row_for(platform)[5]).lower()
        if platform in PROVED_LIVE_INGEST:
            assert not cell.startswith(("no", "not applicable")), (
                f"{platform}: PROVED_LIVE_INGEST says a live ingest was proved, but the "
                f"matrix row still reads {cell!r}"
            )
            continue
        assert cell.startswith("no") or cell.startswith("not applicable"), (
            f"{platform}: the matrix claims {cell!r} but PROVED_LIVE_INGEST does not contain "
            f"it. A live ingest is proved by pushing one stream to that platform and seeing "
            f"it live; the relay proof in this repository was two MediaMTX destinations, "
            f"which is not a platform."
        )

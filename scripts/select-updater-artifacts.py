#!/usr/bin/env python3
"""Select the signed updater artifact for each Tauri target from downloaded files.

The updater manifest may only reference artifacts that a user can actually
download, and the updater refuses an install whose minisign signature does not
match `plugins.updater.pubkey`. So for every platform this script requires both
the artifact and its `.sig` sidecar to be present next to it, and it refuses a
platform whose files are ambiguous rather than guessing.

Two naming shapes are accepted for one platform:

* an explicit target prefix -- ``<platform>-<artifact>`` (for example
  ``darwin-aarch64-xPST.app.tar.gz``), which is what the release workflow
  produces; this always wins;
* the bundler's own names -- ``*.app.tar.gz`` (macOS), ``*setup.exe`` or
  ``*.msi`` (Windows), ``*.AppImage`` (Linux).

**A macOS bundle name must carry its architecture.** ``cargo tauri build``
names the macOS updater archive ``xPST.app.tar.gz`` on every architecture, so
an unmarked name cannot be attributed to a target. Treating "no arm marker" as
"Intel" is how v1.2.1 published an arm64 payload under ``darwin-x86_64``: the
manifest then carried the arm64 digest for both keys, and Intel Macs were
offered an update that could not verify. An unmarked macOS bundle is therefore
reported as ``unattributable`` and never published, unless its name carries an
architecture marker (``aarch64``/``arm64`` vs ``x86_64``/``x64``/``intel``).

Windows ships one signed updater payload per release, not two: when a release
carries a signed NSIS ``setup.exe`` *and* a signed ``.msi`` (the release lane
builds both), NSIS wins deterministically. Two signed candidates used to be
reported as ``ambiguous``, which kept ``windows-x86_64`` out of the manifest
for an entirely avoidable reason.

A platform with no candidate is reported as ``absent``; one whose candidate is
unmarked or tied is reported as ``unattributable``/``ambiguous``. None of these
aborts the run: the caller publishes the platforms it does have and reports the
rest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PLATFORM_KEYS = (
    "darwin-aarch64",
    "darwin-x86_64",
    "windows-x86_64",
    "linux-x86_64",
)

# Extensions that can be an updater artifact. Explicit <platform>-<name>
# candidates must end in one of these, otherwise release bookkeeping files such
# as ``linux-SHA512SUMS`` would look like Linux updater artifacts.
ARTIFACT_SUFFIXES = (
    ".app.tar.gz",
    ".appimage.tar.gz",
    ".nsis.zip",
    ".appimage",
    ".exe",
    ".msi",
    ".zip",
    ".dmg",
)
_ARTIFACT_SUFFIXES_LOWER = tuple(suffix.lower() for suffix in ARTIFACT_SUFFIXES)

# Architecture markers a bundle name may carry. A macOS archive without one
# cannot be attributed to either darwin target (see the module docstring).
_MACOS_ARM_MARKERS = ("aarch64", "arm64")
_MACOS_INTEL_MARKERS = ("x86_64", "x64", "intel", "amd64")



def _is_signature(path: Path) -> bool:
    return path.name.endswith(".sig")


def _signature_for(path: Path) -> Path:
    return path.with_name(path.name + ".sig")


def explicit_candidates(files: list[Path], platform: str) -> list[Path]:
    """Files named ``<platform>-<artifact>`` with an artifact-ish extension."""
    prefix = platform + "-"
    matches = []
    for path in files:
        if _is_signature(path) or not path.name.startswith(prefix):
            continue
        if path.name == "latest.json":
            continue
        if any(path.name.lower().endswith(suffix) for suffix in _ARTIFACT_SUFFIXES_LOWER):
            matches.append(path)
    return sorted(matches)


def macos_arch_of(name: str) -> str | None:
    """Return ``aarch64``/``x86_64`` for a name that names its architecture.

    ``None`` means the name carries no architecture marker; it must not be
    guessed into either target.
    """
    lower = name.lower()
    arm = any(marker in lower for marker in _MACOS_ARM_MARKERS)
    intel = any(marker in lower for marker in _MACOS_INTEL_MARKERS)
    if arm and intel:
        return None
    if arm:
        return "aarch64"
    if intel:
        return "x86_64"
    return None


def inferred_candidates(files: list[Path], platform: str) -> list[Path]:
    """Bundler-default names, mapped to a target in the manifest."""
    matches: list[Path] = []
    for path in files:
        if _is_signature(path):
            continue
        name = path.name
        lower = name.lower()
        if platform in ("darwin-aarch64", "darwin-x86_64"):
            if not lower.endswith(".app.tar.gz"):
                continue
            if macos_arch_of(name) == platform.removeprefix("darwin-"):
                matches.append(path)
        elif platform == "windows-x86_64":
            if lower.endswith(("setup.exe", ".msi", ".nsis.zip", ".msi.zip")):
                matches.append(path)
        elif platform == "linux-x86_64":
            if lower.endswith((".appimage", ".appimage.tar.gz")):
                matches.append(path)
    return sorted(matches)


def _preference_rank(platform: str, path: Path) -> int:
    """Lower is better; one deterministic payload per platform.

    * updater archives (which are what ``createUpdaterArtifacts`` signs in the
      v1-compatible layout) beat raw installers;
    * on Windows the NSIS installer is the updater's install mode, so it beats
      the WiX ``.msi`` when both are signed;
    * on Linux the plain AppImage is what the updater replaces.
    """
    name = path.name.lower()
    if platform == "windows-x86_64":
        if name.endswith(".nsis.zip"):
            return 0
        if name.endswith("setup.exe"):
            return 1
        if name.endswith(".msi"):
            return 2
        if name.endswith(".zip"):
            return 1
    if platform == "linux-x86_64":
        if name.endswith(".appimage.tar.gz"):
            return 0
        if name.endswith(".appimage"):
            return 1
    if platform.startswith("darwin-"):
        if name.endswith(".app.tar.gz"):
            return 0
    return 0


def _prefer_updater_archives(candidates: list[Path], platform: str) -> list[Path]:
    """Keep only the best-ranked candidate group for one platform."""
    if not candidates:
        return []
    ranked = sorted(candidates, key=lambda path: (_preference_rank(platform, path), path.name))
    best = _preference_rank(platform, ranked[0])
    return [path for path in ranked if _preference_rank(platform, path) == best]


def _unattributable_macos_bundles(files: list[Path]) -> list[str]:
    """Bare ``*.app.tar.gz`` names that name no architecture."""
    found: list[str] = []
    for path in files:
        if _is_signature(path):
            continue
        lower = path.name.lower()
        if lower.endswith(".app.tar.gz") and macos_arch_of(path.name) is None:
            found.append(str(path))
    return sorted(found)


def select(platform: str, files: list[Path]) -> dict[str, Any]:
    """Resolve one platform to a signed artifact, or explain why it cannot be."""
    explicit = explicit_candidates(files, platform)
    candidates = _prefer_updater_archives(
        explicit or inferred_candidates(files, platform), platform
    )
    source = "explicit-prefix" if explicit else "bundler-name"
    if not candidates:
        excerpt = _unattributable_macos_bundles(files) if platform.startswith("darwin-") else []
        if excerpt:
            return {
                "platform": platform,
                "status": "unattributable",
                "candidates": excerpt,
                "source": source,
                "reason": (
                    "macOS updater bundle name carries no architecture marker; it cannot be "
                    "attributed to darwin-aarch64 or darwin-x86_64 (rename it to "
                    "<platform>-<name> in the release lane)"
                ),
            }
        return {"platform": platform, "status": "absent", "candidates": [], "source": source}
    if len(candidates) > 1:
        return {
            "platform": platform,
            "status": "ambiguous",
            "candidates": [str(path) for path in candidates],
            "source": source,
        }
    artifact = candidates[0]
    signature = _signature_for(artifact)
    if not signature.is_file():
        return {
            "platform": platform,
            "status": "unsigned",
            "candidates": [str(artifact)],
            "missing_signature": str(signature),
            "source": source,
        }
    if not signature.read_text(encoding="utf-8").strip():
        return {
            "platform": platform,
            "status": "empty-signature",
            "candidates": [str(artifact)],
            "missing_signature": str(signature),
            "source": source,
        }
    return {
        "platform": platform,
        "status": "ok",
        "artifact": str(artifact),
        "signature": str(signature),
        "source": source,
    }


def select_all(directory: Path) -> list[dict[str, Any]]:
    files = sorted(path for path in directory.iterdir() if path.is_file())
    return [select(platform, files) for platform in PLATFORM_KEYS]


def _report_notes(results: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    for result in results:
        if result["status"] == "ok":
            source = result["source"]
            notes.append(f"{result['platform']}: signed artifact ({source})")
        elif result["status"] == "absent":
            notes.append(f"{result['platform']}: no updater artifact in the release")
        elif result["status"] == "ambiguous":
            names = ", ".join(Path(path).name for path in result["candidates"])
            notes.append(f"{result['platform']}: ambiguous, needs an explicit target prefix: {names}")
        elif result["status"] == "unattributable":
            names = ", ".join(Path(path).name for path in result["candidates"])
            notes.append(
                f"{result['platform']}: no architecture in the macOS bundle name, refusing to guess: {names}"
            )
        elif result["status"] == "unsigned":
            notes.append(
                f"{result['platform']}: artifact present but {Path(str(result['missing_signature'])).name} is missing"
            )
        else:
            notes.append(f"{result['platform']}: {result['status']} ({result['candidates'][0]})")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True, help="directory holding downloaded release assets")
    parser.add_argument("--report", type=Path, help="write the full JSON report here")
    parser.add_argument("--args-file", type=Path, help="write PLATFORM=PATH lines for the found platforms here")
    parser.add_argument(
        "--github-output",
        type=Path,
        help="append found/platforms/missing values to this GitHub Actions output file",
    )
    args = parser.parse_args(argv)

    if not args.dir.is_dir():
        raise SystemExit(f"asset directory not found: {args.dir}")

    results = select_all(args.dir)
    found = [result for result in results if result["status"] == "ok"]
    missing = [str(result["platform"]) for result in results if result["status"] != "ok"]
    notes = _report_notes(results)

    report = {
        "found": len(found),
        "platforms": [str(result["platform"]) for result in found],
        "missing": missing,
        "notes": notes,
        "results": results,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.args_file:
        args.args_file.parent.mkdir(parents=True, exist_ok=True)
        args.args_file.write_text(
            "".join(f"{result['platform']}={result['artifact']}\n" for result in found),
            encoding="utf-8",
        )
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"found={len(found)}\n")
            handle.write(f"platforms={','.join(report['platforms'])}\n")
            handle.write(f"missing={','.join(missing)}\n")

    for note in notes:
        print(f"select-updater-artifacts: {note}")
    print(f"select-updater-artifacts: {len(found)} of {len(PLATFORM_KEYS)} platform(s) publishable")
    return 0


if __name__ == "__main__":
    sys.exit(main())

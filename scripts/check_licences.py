#!/usr/bin/env python3
"""Enforce the licence policy (docs/technical/licence-policy.md) on the lockfile.

This is the mechanism that exists today: it reads the committed `Cargo.lock`,
asks Cargo for the licence each locked package declares in its own manifest
(`cargo metadata --offline --locked`, so the answer comes from the registry
metadata already cached on disk, never from a hand-written list), evaluates the
SPDX expression against the accept list, and exits non-zero on anything that is
not acceptable.

Why this and not only cargo-deny: cargo-deny is the richer gate (bans,
duplicates, sources, advisories) and `deny.toml` is checked in for it, but it is
a separately installed binary. This script needs nothing beyond Cargo itself, so
the promise in the policy is testable on any machine that can build the
workspace — including the CI job that runs it on every pull request.

Exit codes:
  0  every locked package is acceptable
  1  a package is not acceptable, or the lockfile could not be read
  2  the script itself could not run (missing Cargo, bad arguments)

Usage:
    python3 scripts/check_licences.py                 # gate: root Cargo.lock
    python3 scripts/check_licences.py --json          # machine-readable report
    python3 scripts/check_licences.py --lockfile src-tauri/Cargo.lock
    python3 scripts/check_licences.py --allow-network # cold cache, first fetch
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Licences this project may ship. Mirrors `deny.toml` and the policy document;
#: `--check-config` (run by the test suite) fails if the three disagree.
ACCEPTED: dict[str, str] = {
    "MIT": "policy",
    "Apache-2.0": "policy",
    "BSD-2-Clause": "policy",
    "BSD-3-Clause": "policy",
    "ISC": "policy",
    "Zlib": "policy",
    "Unlicense": "policy",
    "CDLA-Permissive-2.0": "policy — certificate data, must be named in NOTICE",
    "Unicode-3.0": (
        "Unicode License v3 — permissive, MIT-like; unavoidable transitively "
        "through unicode-ident, which proc-macro2/syn require"
    ),
    "MPL-2.0": (
        "policy — accepted only as an unmodified separate crate; patching one "
        "makes the patch publishable (see the policy's MPL clause)"
    ),
    "CC0-1.0": (
        "public-domain dedication (tiny-keccak, via the Tauri stack); strictly "
        "less restrictive than MIT"
    ),
}

#: SPDX exception identifiers that may appear after WITH. An exception is only
#: accepted on the licence the policy names it for, never on any licence.
ACCEPTED_EXCEPTIONS: dict[str, frozenset[str]] = {
    "LLVM-exception": frozenset({"Apache-2.0"}),
}

#: Whole expressions the policy accepts verbatim, kept next to the bare list so
#: `deny.toml` can be compared without a combinatorial guess.
ACCEPTED_EXPRESSIONS: dict[str, str] = {
    "Apache-2.0 WITH LLVM-exception": "policy — accepted as written",
}

#: Prefixes that must never be satisfied, whatever they are OR-ed with.
REJECTED_PREFIXES = (
    "GPL-",
    "AGPL-",
    "LGPL-",
    "SSPL-",
    "BUSL-",
    "Commons-Clause",
    "LicenseRef-",
)

#: Cargo reports legacy non-SPDX expressions with `/` as the separator, where it
#: meant "or". Normalise before tokenising.
_LEGACY_OR = re.compile(r"\s*/\s*")
_TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.\-+:]+")


class PolicyError(Exception):
    """A package's licence cannot be shown to satisfy the policy."""


def tokenize(expression: str) -> list[str]:
    normalised = _LEGACY_OR.sub(" OR ", expression.strip())
    tokens = _TOKEN.findall(normalised)
    joined = "".join(tokens)
    if re.sub(r"\s+", "", normalised) != joined:
        raise PolicyError(f"unparseable licence expression {expression!r}")
    return tokens


class Evaluator:
    """Decide whether an SPDX expression can be satisfied by accepted licences.

    `AND` needs every operand acceptable, `OR` needs one — which is what the
    policy means: we may take a crate offered under `MIT OR Apache-2.0` because
    either branch is fine for us, and we may not take `MIT AND GPL-3.0`.
    """

    def __init__(self, expression: str) -> None:
        self.expression = expression
        self.tokens = tokenize(expression)
        self.position = 0

    def evaluate(self) -> tuple[bool, list[str]]:
        """Return (acceptable, unacceptable identifiers)."""
        if not self.tokens:
            raise PolicyError(f"empty licence expression {self.expression!r}")
        acceptable, offenders = self._parse_or()
        if self.position != len(self.tokens):
            raise PolicyError(
                f"trailing tokens in licence expression {self.expression!r}: "
                f"{self.tokens[self.position:]}"
            )
        return acceptable, offenders

    def _parse_or(self) -> tuple[bool, list[str]]:
        acceptable, offenders = self._parse_and()
        while self._peek() == "OR":
            self.position += 1
            right_ok, right_offenders = self._parse_and()
            if acceptable or right_ok:
                acceptable, offenders = True, []
            else:
                offenders = sorted(set(offenders) | set(right_offenders))
        return acceptable, offenders

    def _parse_and(self) -> tuple[bool, list[str]]:
        acceptable, offenders = self._parse_atom()
        while self._peek() == "AND":
            self.position += 1
            right_ok, right_offenders = self._parse_atom()
            acceptable = acceptable and right_ok
            offenders = sorted(set(offenders) | set(right_offenders))
        return acceptable, offenders

    def _parse_atom(self) -> tuple[bool, list[str]]:
        token = self._peek()
        if token is None:
            raise PolicyError(f"licence expression ended early: {self.expression!r}")
        if token == "(":
            self.position += 1
            acceptable, offenders = self._parse_or()
            if self._peek() != ")":
                raise PolicyError(f"unbalanced parenthesis in {self.expression!r}")
            self.position += 1
            return acceptable, offenders
        if token in {")", "AND", "OR", "WITH"}:
            raise PolicyError(f"unexpected {token!r} in {self.expression!r}")
        self.position += 1

        identifier = token
        exception = None
        if self._peek() == "WITH":
            self.position += 1
            exception = self._peek()
            if exception is None:
                raise PolicyError(f"WITH with no exception in {self.expression!r}")
            self.position += 1

        rejected = identifier.upper().startswith(REJECTED_PREFIXES)
        accepted_id = identifier in ACCEPTED
        if exception is not None:
            allows = ACCEPTED_EXCEPTIONS.get(exception, frozenset())
            accepted_id = accepted_id and identifier in allows
            rejected = rejected or exception.upper().startswith(REJECTED_PREFIXES)
        label = identifier if exception is None else f"{identifier} WITH {exception}"
        if rejected or not accepted_id:
            return False, [label]
        return True, []

    def _peek(self) -> str | None:
        if self.position >= len(self.tokens):
            return None
        return self.tokens[self.position]


def lockfile_packages(path: Path) -> list[tuple[str, str, str | None]]:
    """Every `[[package]]` in a lockfile as (name, version, source)."""
    text = path.read_text(encoding="utf-8")
    packages: list[tuple[str, str, str | None]] = []
    for block in text.split("[[package]]")[1:]:
        name = re.search(r'^name = "(.*)"$', block, re.MULTILINE)
        version = re.search(r'^version = "(.*)"$', block, re.MULTILINE)
        if not name or not version:
            continue
        source = re.search(r'^source = "(.*)"$', block, re.MULTILINE)
        packages.append((name.group(1), version.group(1), source.group(1) if source else None))
    if not packages:
        raise PolicyError(f"{path} lists no packages — refusing to pass vacuously")
    return packages


def cargo_metadata(manifest_dir: Path, allow_network: bool) -> dict:
    command = ["cargo", "metadata", "--locked", "--format-version", "1"]
    if not allow_network:
        command.append("--offline")
    completed = subprocess.run(
        command,
        cwd=manifest_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        tail = "\n  ".join(detail[-6:]) if detail else "no output"
        raise PolicyError(
            f"`{' '.join(command)}` failed in {manifest_dir}:\n  {tail}\n"
            "  (a cold cargo cache needs `cargo fetch --locked` first, or --allow-network)"
        )
    return json.loads(completed.stdout)


def licence_map(manifest_dir: Path, allow_network: bool) -> dict[tuple[str, str], str]:
    metadata = cargo_metadata(manifest_dir, allow_network)
    licences: dict[tuple[str, str], str] = {}
    for package in metadata.get("packages", []):
        expression = package.get("license") or ""
        if not expression:
            licence_file = package.get("license_file")
            expression = f"LicenseRef-file:{licence_file}" if licence_file else "NOASSERTION"
        licences[(package["name"], package["version"])] = expression
    if not licences:
        raise PolicyError("cargo metadata reported no packages — refusing to pass vacuously")
    return licences


def check(lockfile: Path, allow_network: bool) -> list[dict]:
    """Evaluate every locked package. Returns the report rows."""
    manifest_dir = lockfile.parent
    licences = licence_map(manifest_dir, allow_network)
    report: list[dict] = []
    for name, version, source in lockfile_packages(lockfile):
        expression = licences.get((name, version))
        if source is None:
            # A path member: the licence is the workspace's own grant, and it is
            # checked like any other rather than waved through — a member that
            # declares nothing is exactly the hole this script exists to close.
            if expression is None:
                report.append(
                    {
                        "name": name,
                        "version": version,
                        "licence": "unlicensed",
                        "accepted": False,
                        "detail": "path member with no licence field in its manifest",
                    }
                )
                continue
        if expression is None:
            report.append(
                {
                    "name": name,
                    "version": version,
                    "licence": None,
                    "accepted": False,
                    "detail": "locked, but cargo metadata reported no such package",
                }
            )
            continue
        try:
            acceptable, offenders = Evaluator(expression).evaluate()
            detail = "" if acceptable else "not acceptable under the policy: " + ", ".join(offenders)
        except PolicyError as error:
            acceptable, detail = False, str(error)
        report.append(
            {
                "name": name,
                "version": version,
                "licence": expression,
                "accepted": acceptable,
                "detail": detail,
            }
        )
    return report


def check_deny_toml(root: Path) -> list[str]:
    """Fail if deny.toml's allow list has drifted from this script's.

    Two lists that must agree are two lists that will disagree; this is how the
    disagreement becomes a test failure instead of a licence nobody reviewed.
    """
    path = root / "deny.toml"
    if not path.exists():
        return ["deny.toml is missing: cargo-deny would have no policy to enforce"]
    text = path.read_text(encoding="utf-8")
    section = re.search(r"^\[licenses\](.*?)(?=^\[)", text, re.MULTILINE | re.DOTALL)
    if not section:
        return ["deny.toml has no [licenses] section"]
    allow = re.search(r"^allow\s*=\s*\[(.*?)\]", section.group(1), re.MULTILINE | re.DOTALL)
    if not allow:
        return ["deny.toml [licenses] has no allow list"]
    listed = set(re.findall(r'"([^"]+)"', allow.group(1)))
    # An entry of the form `A WITH B` grants an expression, not a bare licence.
    expressions = {entry for entry in listed if " WITH " in entry}
    bare = listed - expressions
    expected_expressions = set(ACCEPTED_EXPRESSIONS)
    problems = []
    for missing in sorted(set(ACCEPTED) - bare):
        problems.append(f"deny.toml does not allow {missing!r}, which check_licences.py accepts")
    for missing in sorted(expected_expressions - expressions):
        problems.append(f"deny.toml does not allow {missing!r}, which check_licences.py accepts")
    for extra in sorted(expressions - expected_expressions):
        problems.append(
            f"deny.toml allows {extra!r}, which check_licences.py does not review"
        )
    for extra in sorted(bare - set(ACCEPTED)):
        problems.append(
            f"deny.toml allows {extra!r}, which check_licences.py would reject as unreviewed"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help="lockfile to check, relative to the root (default: Cargo.lock)",
    )
    parser.add_argument("--allow-network", action="store_true", help="let cargo fetch metadata")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--check-config", action="store_true", help="also compare deny.toml against this list"
    )
    arguments = parser.parse_args(argv)

    root = arguments.root.resolve()
    lockfile = (arguments.lockfile or Path("Cargo.lock"))
    if not lockfile.is_absolute():
        lockfile = root / lockfile
    if not lockfile.exists():
        print(f"::error::{lockfile} does not exist", file=sys.stderr)
        return 1

    problems = check_deny_toml(root) if arguments.check_config else []
    try:
        report = check(lockfile, arguments.allow_network)
    except PolicyError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"::error::could not read {lockfile}: {error}", file=sys.stderr)
        return 2

    violations = [row for row in report if not row["accepted"]]
    if arguments.json:
        print(
            json.dumps(
                {
                    "lockfile": str(lockfile),
                    "packages": len(report),
                    "violations": violations,
                    "config_problems": problems,
                    "accepted": sorted(ACCEPTED),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        if violations:
            print(f"{len(violations)} package(s) outside the licence policy:\n")
            for row in violations:
                print(f"  {row['name']} {row['version']}: {row['licence']}")
                print(f"    {row['detail']}")
            print()
        for problem in problems:
            print(f"  policy drift: {problem}")
        if not violations and not problems:
            expressions = sorted({row["licence"] for row in report})
            print(
                f"licences ok: {len(report)} packages in {lockfile.name} "
                f"under {len(expressions)} declared licence expressions"
            )
            print(f"  accepted: {', '.join(sorted(ACCEPTED))}")
    if violations or problems:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

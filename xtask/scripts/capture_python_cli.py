"""Capture the Python xPST CLI command tree into the parity manifest that the
Rust `xpst-cli` crate is built from.

Usage (from the repo root of an isolated worktree of origin/main):

    PYTHONPATH="$PWD/src" python xtask/scripts/capture_python_cli.py \
        > crates/xpst-cli/src/parity/cli_parity.json

The output is the *oracle* for P0 command-tree parity: `cargo test -p
xpst-cli` asserts the Rust `--help` covers every command path and every flag
recorded here. Nothing in the manifest is hand-edited; re-run this script when
`src/xpst/cli.py` changes and review the diff like any other contract change.

No personal data is captured: only command names, parameter names, help text
and defaults from the click tree.
"""

from __future__ import annotations

import json
import subprocess
import sys

import click

sys.path.insert(0, "src")  # noqa: PTH  (explicit: introspect the tree under test)

import xpst.cli as cli_mod  # noqa: E402


def _git_ref() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def _param(p: click.Parameter) -> dict:
    opts = list(getattr(p, "opts", []))
    secondary = list(getattr(p, "secondary_opts", []))
    is_argument = isinstance(p, click.Argument)
    is_flag = bool(getattr(p, "is_flag", False))
    return {
        "name": p.name,
        "kind": "argument" if is_argument else "option",
        "opts": opts,
        "secondary_opts": secondary,
        "is_flag": is_flag,
        "multiple": bool(getattr(p, "multiple", False)),
        "required": bool(p.required),
        "nargs": int(getattr(p, "nargs", 1)),
        "metavar": getattr(p, "metavar", None),
        "type": str(getattr(getattr(p, "type", None), "name", "")),
        "choices": _choices(p),
        # A flag's "default" needs normalizing, not dropping: Click 8.5 returns an
        # UNSET sentinel for an undeclared flag default while older versions
        # return False, so recording the raw value would make the manifest
        # depend on the installed Click. A flag is on-by-default only when it was
        # explicitly declared that way (`--merge`, `--api-only` today), and every
        # other case is False in every Click version.
        "default": _flag_default(p) if is_flag else _jsonable(p.default if not callable(p.default) else None),
        "help": (getattr(p, "help", "") or "").strip(),
    }


def _jsonable(value):
    # click's UNSET sentinel and any other non-JSON object collapse to null so
    # the manifest stays plain JSON. A null default means "no default declared",
    # exactly like an omitted click default.
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def _choices(p: click.Parameter):
    choices = getattr(getattr(p, "type", None), "choices", None)
    return [str(c) for c in choices] if choices else None


def _flag_default(p: click.Parameter) -> bool:
    """On-by-default flag, in a form that does not vary by Click version.

    Click's UNSET sentinel is not a bool, so `is True` is the only comparison
    that survives both the sentinel-era and the False-era behaviour.
    """
    return p.default is True


def _command(cmd: click.Command, path: list[str]) -> dict:
    node = {
        "path": path,
        "name": cmd.name,
        "help": (cmd.get_short_help_str(limit=200) or "").strip(),
        "about": (cmd.help or "").strip(),
        "params": [
            _param(p) for p in cmd.params if p.name not in {"help", None}
        ],
        "subcommands": [],
    }
    if isinstance(cmd, click.Group):
        for name in sorted(cmd.commands):
            node["subcommands"].append(_command(cmd.commands[name], [*path, name]))
    return node


def _flatten(node: dict, out: list[dict]) -> None:
    out.append({k: v for k, v in node.items() if k != "subcommands"})
    for child in node["subcommands"]:
        _flatten(child, out)


root = _command(cli_mod.main, [])
flat: list[dict] = []
_flatten(root, flat)

manifest = {
    "schema_version": 1,
    "captured_from": {
        "repo": "TysAIs/xPST",
        "git_ref": _git_ref(),
        "source": "src/xpst/cli.py",
        "framework": "click",
        "captured_by": "xtask/scripts/capture_python_cli.py",
    },
    "note": (
        "Generated contract: the Rust engine must expose every command path and "
        "flag listed here. Do not hand-edit; re-run the capture script. "
        "`captured_from` is provenance only and is excluded from parity "
        "comparisons, which look at `commands` alone."
    ),
    "command_count": len(flat),
    "commands": flat,
}

json.dump(manifest, sys.stdout, indent=1, sort_keys=False)
sys.stdout.write("\n")

"""Audit every ``retryable=`` declaration in the engine, as a markdown table.

Retry ownership depends on the adapters' declarations being honest, so this
walks the AST instead of the text: a grep audit silently misses sites whose
error text is assembled over several lines.

Usage:
    python scripts/audit_retryable_sites.py

Exit code is 0 even when contradictions are found — the CI gate for those is
``tests/test_retry_ownership_contract.py``; this script is the human report.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

#: Same markers ``retry_operation`` used to key on before the contract landed.
RETRY_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttl",
    "temporarily",
    "try again",
)


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    name = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                name = node.name
    return name


def _classify(value: ast.AST | None) -> tuple[str, str]:
    """Return (shape, static text) for an ``error=`` argument."""
    if value is None:
        return "none", ""
    if isinstance(value, ast.Constant):
        return "static", str(value.value)
    if isinstance(value, ast.JoinedStr):
        text = "".join(
            str(part.value) for part in value.values if isinstance(part, ast.Constant)
        )
        dynamic = any(not isinstance(part, ast.Constant) for part in value.values)
        return ("mixed" if dynamic else "static"), text
    return "dynamic", ""


def collect() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "UploadResult":
                continue
            flag = None
            error_node = None
            for kw in node.keywords:
                if kw.arg == "retryable" and isinstance(kw.value, ast.Constant):
                    flag = kw.value.value
                elif kw.arg == "error":
                    error_node = kw.value
            if flag is None:
                continue
            shape, text = _classify(error_node)
            rows.append(
                {
                    "file": str(path.relative_to(ROOT)),
                    "line": node.lineno,
                    "func": _enclosing_function(tree, node.lineno),
                    "flag": flag,
                    "shape": shape,
                    "keywords": ", ".join(
                        marker for marker in RETRY_MARKERS if marker in text.lower()
                    ),
                    "text": " ".join(text.split())[:110],
                }
            )
    return rows


def main() -> int:
    rows = collect()
    print("| site | function | declared | error text | retryable-looking? |")
    print("|---|---|---|---|---|")
    for row in rows:
        keywords = row["keywords"] or "-"
        print(
            f"| {row['file']}:{row['line']} | `{row['func']}` | {row['flag']} | "
            f"[{row['shape']}] {row['text']} | {keywords} |"
        )
    terminal = [r for r in rows if r["flag"] is False]
    contradictions = [
        f"{r['file']}:{r['line']}" for r in terminal if r["keywords"]
    ]
    print()
    print(f"declarations: {len(rows)} (retryable=False: {len(terminal)}, "
          f"retryable=True: {sum(1 for r in rows if r['flag'] is True)})")
    print(f"static-text contradictions (False + keyword): {contradictions or 'none'}")
    print(
        "dynamic/mixed texts (the declaration is the only signal): "
        f"{sum(1 for r in terminal if r['shape'] != 'static')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

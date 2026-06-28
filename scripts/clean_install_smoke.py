#!/usr/bin/env python3
"""Clean-install smoke test for built xPST Python artifacts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path


def find_wheel(dist_dir: Path) -> Path:
    """Return the newest xPST wheel in a dist directory."""
    wheels = sorted(dist_dir.glob("xpst-*.whl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not wheels:
        raise FileNotFoundError(f"No xpst wheel found in {dist_dir}")
    return wheels[0]


def find_sdist(dist_dir: Path) -> Path:
    """Return the newest xPST source distribution in a dist directory."""
    sdists = sorted(dist_dir.glob("xpst-*.tar.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not sdists:
        raise FileNotFoundError(f"No xpst sdist found in {dist_dir}")
    return sdists[0]


def venv_python(venv_dir: Path) -> Path:
    """Return the Python executable path for a virtual environment."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_xpst(venv_dir: Path) -> Path:
    """Return the xpst entrypoint path for a virtual environment."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "xpst.exe"
    return venv_dir / "bin" / "xpst"


def run_command(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a command and capture text output."""
    return subprocess.run(cmd, text=True, capture_output=True, env=env, timeout=120, check=False)


def json_from_output(output: str) -> object:
    """Parse JSON from command output that may include log lines."""
    stripped = output.strip()
    for index, char in enumerate(stripped):
        if char in "[{":
            return json.loads(stripped[index:])
    return json.loads(stripped)


def assert_command_json(cmd: list[str], env: dict[str, str]) -> object:
    """Run a command and assert it exits successfully with JSON output."""
    result = run_command(cmd, env)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    try:
        return json_from_output(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Command did not emit valid JSON: {' '.join(cmd)}\n{result.stdout}") from exc


def write_smoke_config(work_dir: Path) -> Path:
    """Write a minimal config that avoids real user state."""
    config_dir = work_dir / "xpst-home"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        f"""accounts:
  tiktok:
    username: smoke_creator
  youtube:
    enabled: false
    client_secrets: ""
    token_file: ""
  x:
    enabled: false
    cookies_file: ""
  instagram:
    enabled: false
    session_file: ""
    username: ""
  local:
    path: ""
video:
  download_dir: "{(config_dir / "downloads").as_posix()}"
monitoring:
  log_level: INFO
  log_file: "{(config_dir / "logs" / "xpst.log").as_posix()}"
reliability:
  max_retries: 3
rate_limits:
  youtube: 5
  instagram: 5
  x: 5
  tiktok: 5
schedule:
  check_interval: 900
""",
        encoding="utf-8",
    )
    return config_path


def _select_artifacts(dist_dir: Path, artifact: str) -> list[Path]:
    if artifact == "wheel":
        return [find_wheel(dist_dir)]
    if artifact == "sdist":
        return [find_sdist(dist_dir)]
    if artifact == "both":
        return [find_wheel(dist_dir), find_sdist(dist_dir)]
    raise ValueError(f"Unsupported artifact type: {artifact}")


def run_clean_install_smoke(
    dist_dir: Path,
    work_dir: Path,
    keep: bool = False,
    artifact: str = "wheel",
) -> dict[str, object]:
    """Install Python artifacts into fresh virtual environments and smoke core commands."""
    artifacts = _select_artifacts(dist_dir, artifact)
    results = [_smoke_artifact(path, work_dir / path.stem.replace(".", "-"), keep=keep) for path in artifacts]
    return {
        "ok": True,
        "artifacts": results,
        "commands": results[0]["commands"] if results else [],
        "xpst_version": results[0]["xpst_version"] if results else None,
    }


def _smoke_artifact(artifact_path: Path, work_dir: Path, keep: bool = False) -> dict[str, object]:
    """Install one artifact into a fresh virtual environment and smoke core commands."""
    venv_dir = work_dir / "venv"
    work_dir.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python = venv_python(venv_dir)
    xpst = venv_xpst(venv_dir)
    env = os.environ.copy()
    env["XPST_LOG_LEVEL"] = "WARNING"
    env["PYTHONUTF8"] = "1"

    install = run_command([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(artifact_path)], env)
    if install.returncode != 0:
        raise RuntimeError(f"Artifact install failed for {artifact_path.name}:\n{install.stdout}\n{install.stderr}")

    config_path = write_smoke_config(work_dir)
    diagnostics_path = work_dir / "diagnostics.zip"
    commands = {
        "version": [str(xpst), "version", "--json"],
        "providers": [str(xpst), "--config", str(config_path), "providers", "--json"],
        "updates": [str(xpst), "update", "--components", "--json"],
        "readiness": [str(xpst), "--config", str(config_path), "readiness", "--json"],
        "diagnostics": [
            str(xpst),
            "--config",
            str(config_path),
            "diagnostics",
            "--output",
            str(diagnostics_path),
            "--json",
        ],
    }
    outputs = {name: assert_command_json(cmd, env) for name, cmd in commands.items()}

    if not diagnostics_path.exists():
        raise RuntimeError("Diagnostics bundle was not created")
    with zipfile.ZipFile(diagnostics_path) as archive:
        entries = set(archive.namelist())
    if entries != {"README.txt", "diagnostics.json"}:
        raise RuntimeError(f"Unexpected diagnostics bundle contents: {sorted(entries)}")

    return {
        "ok": True,
        "artifact": str(artifact_path),
        "artifact_type": "sdist" if artifact_path.name.endswith(".tar.gz") else "wheel",
        "venv": str(venv_dir) if keep else None,
        "commands": sorted(outputs),
        "diagnostics_entries": sorted(entries),
        "xpst_version": outputs["version"].get("xpst") if isinstance(outputs["version"], dict) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test built xPST Python artifacts in clean virtual environments")
    parser.add_argument("--dist", default="dist", help="Directory containing built Python artifacts")
    parser.add_argument(
        "--artifact",
        choices=["wheel", "sdist", "both"],
        default="wheel",
        help="Artifact type to install",
    )
    parser.add_argument("--work-dir", default=None, help="Working directory to use instead of a temporary directory")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary smoke environment")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    dist_dir = Path(args.dist).resolve()
    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="xpst-clean-install-"))
        cleanup = not args.keep

    try:
        result = run_clean_install_smoke(dist_dir, work_dir, keep=args.keep, artifact=args.artifact)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Clean-install smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        artifacts = result.get("artifacts", [])
        labels = ", ".join(Path(str(item["artifact"])).name for item in artifacts if isinstance(item, dict))
        print(f"Clean-install smoke passed for {labels}")
        print(f"Commands: {', '.join(result['commands'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

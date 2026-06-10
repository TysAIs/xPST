# Contributing to xPST

Thank you for helping make xPST better. The project goal is a free, open-source,
local-first cross-posting studio that creators can trust without subscribing to
a hosted service.

## Quick Start

Requirements:

- Python 3.10 or newer
- FFmpeg
- Git

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -e ".[dev,mcp]"
xpst readiness --json
```

## Before Opening an Issue

- Search existing issues first.
- Never paste tokens, cookies, session files, OAuth secrets, or raw credential
  files.
- For setup problems, run `xpst diagnostics --json` and review the generated
  bundle before attaching it.
- Use the platform breakage, install failure, or provider request templates when
  they match your report.

Security vulnerabilities should follow [SECURITY.md](SECURITY.md), not public
issues.

## Pull Requests

1. Fork the repository.
2. Create a focused branch.
3. Keep unrelated refactors out of the PR.
4. Add or update tests for behavior changes.
5. Update docs when user-facing commands, configuration, provider behavior, or
   release steps change.
6. Run the relevant checks before opening the PR.

Recommended local checks:

```bash
pytest
ruff check src tests scripts/verify_qml_pages.py scripts/release_artifacts.py scripts/clean_install_smoke.py
mypy src/xpst scripts/release_artifacts.py scripts/clean_install_smoke.py
python scripts/verify_qml_pages.py
python -m build
python scripts/clean_install_smoke.py --dist dist --artifact both
python scripts/release_artifacts.py --dist dist --output-dir release --skip-checks
```

## Provider Contributions

xPST is provider-agnostic. New sources and destinations should expose a
machine-readable provider manifest so CLI, desktop, MCP, diagnostics, and
release tooling can reason about capabilities consistently.

For a new destination provider:

1. Add a module under `src/xpst/platforms/`.
2. Inherit from the existing platform base class.
3. Implement upload, health, and any supported delete/analytics behavior.
4. Add a `ProviderManifest` with roles, capabilities, auth mode, docs URL, and
   risk notes.
5. Add tests for the provider contract and failure isolation.

For a new source provider:

1. Add a module under `src/xpst/sources/`.
2. Inherit from the existing source base class.
3. Implement listing, download, health, and capability metadata.
4. Add fake-provider or mocked tests for auth, rate limits, network failures,
   and malformed API responses.

Provider integrations must be honest about official API status and platform
Terms risk.

## Coding Guidelines

- Prefer existing project patterns over new abstractions.
- Keep user data local by default.
- Avoid logging captions, credentials, cookies, tokens, or raw local paths.
- Use structured JSON outputs for commands that may be consumed by agents or
  scripts.
- Keep platform-specific behavior inside provider/source adapters rather than
  the engine, desktop UI, updater, or state layer.

## Testing Expectations

Tests should cover:

- Success and failure paths.
- Provider failure isolation.
- JSON command output contracts.
- Config migration and corrupted-file behavior when relevant.
- State persistence, retries, and recovery for workflow changes.
- Redaction for diagnostics or logs that include user-controlled text.

Use fake providers where possible instead of relying on live platform accounts.

## Documentation

Update the relevant docs when behavior changes:

- `README.md` for user-facing commands and feature claims.
- `docs/AGENT_GUIDE.md` for JSON/automation workflows.
- `docs/MCP_TOOLS.md` for MCP tools and resources.
- `docs/LAUNCH_CHECKLIST.md` and `docs/ENTERPRISE_READINESS.md` for release
  gates.
- Provider-specific docs for auth, rate limits, and platform caveats.

## Release Notes

User-visible changes should be reflected in `CHANGELOG.md`. Release artifacts
must include checksums, SBOM, notices, license information, and release notes.

## Community

- Issues: https://github.com/TysAIs/xPST/issues
- Discussions: https://github.com/TysAIs/xPST/discussions
- Pull requests: https://github.com/TysAIs/xPST/pulls

Be respectful, specific, and practical. Small focused improvements are very
welcome.

# xPST Licensing Report

This report summarizes the current repository licensing posture for launch review. It complements [NOTICES.md](NOTICES.md), which lists direct dependency notices.

## Project License

xPST is dual licensed under:

- MIT License
- Apache License 2.0

Users may choose either license. The combined license text is in [LICENSE](LICENSE).

## Direct Dependency Posture

The direct dependencies declared in `pyproject.toml` are compatible with open-source redistribution when packaged with appropriate notices:

| License family | Direct packages |
|---|---|
| MIT | PyYAML, rich, twikit, instagrapi, keyring, pydantic-settings, mcp, winshell, nicegui, plotly |
| Apache-2.0 | google-api-python-client, google-auth-oauthlib, google-auth-httplib2, prometheus-client, bcrypt |
| Apache-2.0 OR BSD-3-Clause | cryptography |
| MIT OR Apache-2.0 | structlog |
| BSD-3-Clause | click, uvicorn, authlib, httpx, pywebview |
| PSF | pywin32 |
| Unlicense | yt-dlp |
| LGPL/GPL/commercial | PySide6 / Qt |

## Desktop Packaging Notes

PySide6/Qt is the dependency that needs the most care for bundled desktop releases:

- Include Qt/PySide6 copyright and license notices in desktop artifacts.
- Prefer dynamic linking and standard PySide6 redistribution behavior for LGPL compliance.
- Do not statically link Qt unless the release follows GPL-compatible or commercial-license requirements.
- Include source-offer or relinking information if a packaging format requires it.

## Release Artifact Requirements

Every public release should include:

- `LICENSE`
- `NOTICES.md`
- Generated transitive dependency license report for the shipped environment
- SHA-256 checksums for release artifacts
- SBOM, preferably CycloneDX JSON
- Platform-specific notices for bundled Windows/macOS desktop artifacts

## Verification Commands

Run these before release:

```bash
uv run pytest -q --timeout=60 --timeout-method=thread
uv run ruff check src tests scripts/verify_qml_pages.py
uv run mypy src/xpst
uv build
```

For vulnerability and license reporting in CI:

```bash
python -m pip install pip-audit pip-licenses cyclonedx-bom
pip-audit
pip-licenses --format=markdown --with-urls --with-license-file > LICENSING_REPORT.generated.md
cyclonedx-py environment -o xpst-sbom.cdx.json
```

Last updated: June 2026.

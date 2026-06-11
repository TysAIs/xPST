# Third-Party Notices

xPST is dual licensed as `MIT OR Apache-2.0`. This file lists direct runtime and optional distribution dependencies declared in `pyproject.toml`. Transitive dependency notices should be generated for each release artifact from the locked environment used to build that artifact.

## Core Runtime

| Package | Declared range | License | Role |
|---|---:|---|---|
| click | `>=8.1.0` | BSD-3-Clause | CLI command parsing |
| PyYAML | `>=6.0` | MIT | YAML config parsing |
| rich | `>=13.0.0` | MIT | Terminal output |
| yt-dlp | `>=2025.1.1` | Unlicense | Source media extraction/downloads |
| google-api-python-client | `>=2.0.0` | Apache-2.0 | YouTube Data API client |
| google-auth-oauthlib | `>=1.0.0` | Apache-2.0 | YouTube OAuth flow |
| google-auth-httplib2 | `>=0.1.0` | Apache-2.0 | Google auth HTTP transport |
| twikit | `>=2.0.0` | MIT | X cookie/session client |
| instagrapi | `>=2.0.0` | MIT | Instagram session client |
| structlog | `>=23.0.0` | MIT OR Apache-2.0 | Structured logging |
| prometheus-client | `>=0.19.0` | Apache-2.0 | Metrics exposition |
| keyring | `>=25.0.0` | MIT | OS credential storage |
| bcrypt | `>=4.0.0` | Apache-2.0 | Password hashing |
| cryptography | `>=41.0.0` | Apache-2.0 OR BSD-3-Clause | Encrypted credential fallback |
| fastapi | `>=0.100.0` | MIT | Dashboard/API server |
| uvicorn | `>=0.23.0` | BSD-3-Clause | ASGI server |
| httpx | `>=0.24.0` | BSD-3-Clause | HTTP client |
| pydantic-settings | `>=2.0.0` | MIT | Settings helpers |

## Optional Extras

| Package | Extra | Declared range | License | Role |
|---|---|---:|---|---|
| mcp | `mcp` | `>=1.0.0` | MIT | MCP server framework |
| pywebview | `desktop` | `>=4.0` | BSD-3-Clause | Desktop webview fallback |
| pywin32 | `windows` | `>=306` | PSF | Windows integration |
| winshell | `windows` | `>=0.6` | MIT | Windows shortcut/shell helpers |
| nicegui | `dashboard` | `>=1.4.0,<4.0.0` | MIT | Browser dashboard |
| plotly | `dashboard` | `>=5.18.0,<7.0.0` | MIT | Dashboard charts |
| PySide6 | `pyside6` | `>=6.5.0` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Native QML desktop app |
| faster-whisper | `knowledge` | `>=1.0.0` | MIT | Knowledge base transcription |
| fastembed | `knowledge` | `>=0.3` | Apache-2.0 | Knowledge base in-process embeddings |
| lancedb | `knowledge` | `>=0.5` | Apache-2.0 | Knowledge base embedded vector store |

## License Compatibility Summary

The direct dependency set is compatible with xPST's open-source distribution model when packaged carefully:

- MIT, BSD, PSF, Unlicense, and Apache-2.0 dependencies are permissive and compatible with `MIT OR Apache-2.0`.
- `structlog` uses a compatible dual MIT/Apache-2.0 license.
- `PySide6` is available under LGPL/GPL/commercial terms. xPST should distribute PySide6/Qt notices with desktop bundles and avoid static linking unless the release uses a compatible commercial or copyleft strategy.
- Release builds should include generated transitive notices because wheel, desktop, and executable artifacts can include different dependency sets.

## Provider Risk Notice

xPST should not imply that all integrations are official platform APIs:

- YouTube destination uses the official YouTube Data API v3.
- Instagram and X adapters use user-owned sessions/cookies through open-source clients.
- TikTok source workflows use yt-dlp extraction and optional browser cookies.

Users are responsible for complying with platform terms, account policies, and applicable law.

## Regenerating Release Notices

For a release environment, generate a full transitive report after installing the exact extras being shipped:

```bash
python -m pip install ".[full]"
python -m pip install pip-licenses
pip-licenses --format=markdown --with-urls --with-license-file > LICENSING_REPORT.generated.md
```

For Python package releases without desktop extras, install the package/extras being published and regenerate the report from that environment.

Last updated: June 2026.

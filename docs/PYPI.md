# PyPI publication

xPST is published to PyPI by `.github/workflows/publish-pypi.yml` (GitHub
Actions) using the `PYPI_API_TOKEN` repository secret — a PyPI API token scoped
to the `xpst` project. The workflow builds the sdist and wheel, validates both
with `twine check`, and uploads them when a version tag is pushed or a GitHub
release is published.

No maintainer step is needed to publish: the secret is already configured
(`TysAIs/xPST` → **Settings** → **Secrets and variables** → **Actions**), and
the publish job references no protected environment, so nothing waits for a
human approval click. Rotating the token is the only recurring chore, and only
if it is revoked.

## Why a token and not trusted publishing

PyPI Trusted Publishing (OIDC) needs a one-time, account-scoped registration in
the PyPI web UI: **Your projects** → **Publishing** → **Add a pending
publisher** with owner `TysAIs`, repository `xPST`, workflow `publish-pypi.yml`
and environment `pypi`. Until that registration exists, an OIDC publish fails
with `invalid-publisher` — which is exactly why publishing was broken before
the token path was wired up.

Switching to trusted publishing later (it stores no long-lived secret) means:

1. Register the pending publisher above on PyPI.
2. Re-add `id-token: write` to the workflow and publish-job `permissions`.
3. Re-add `environment: pypi` to the publish job.
4. Drop the `password:` input from the publish step.

The workflow file carries the same note next to the publish step.

## Release procedure

1. Update the package version in `pyproject.toml` (and every other version
   surface pinned by `tests/test_tauri_production_assembly.py`) so it matches
   the release tag — version `1.2.2` uses tag `v1.2.2`. The version on `main`
   must be published only once: PyPI versions are immutable and cannot be
   reused, re-uploaded or deleted-and-replaced.
2. Merge the version change to `main`. Publish from a commit on `main`, never
   from a dirty tree or a branch.
3. Push the version tag, or publish a GitHub release for the existing tag:

   ```bash
   git tag v1.2.2
   git push origin v1.2.2
   ```

   The workflow runs on version-tag pushes and on published GitHub releases. A
   `workflow_dispatch` run is build-and-validate only: its publish job is gated
   to tag/release events, so a dispatch cannot prove publishing.
4. Verify the registry and the installed package after the job succeeds — read
   the index back, do not trust the green check alone:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/xpst/json   # 200
   uv venv /tmp/xpst-verify && uv pip install --python /tmp/xpst-verify/bin/python xpst==<version>
   /tmp/xpst-verify/bin/xpst --version
   ```

5. Record the version, the index URL and the install proof in the release
   evidence (`python-RELEASE_EVIDENCE.json` / the release notes).

## Pre-upload artifact check

The sdist and wheel are scanned before upload, not after:

```bash
uv build
uvx --from twine twine check dist/*
python scripts/scan_public_safety.py --artifacts dist --json
```

`release.yml` runs the same scan as part of its gate, so a leak fails the
release before anything reaches the registry.

# PyPI publication

**Status: the `xpst` name is still unclaimed.** `pypi.org/pypi/xpst/json` returns
HTTP 404, so `pip install xpst` does not work yet. The publish path below is
wired and ready; nothing has been uploaded because no version tag has been pushed
since the credential path changed.

## Which workflows touch PyPI

Two workflows in this repository reference PyPI. Only the first is the
authoritative publisher:

- **`.github/workflows/publish-pypi.yml` — the publisher.** It builds the sdist
  and wheel, validates both with `twine check`, and uploads them to PyPI. The
  upload job runs only on a `v*.*.*` tag push or a published GitHub release. A
  `workflow_dispatch` run is build-and-validate only: the publish job is gated to
  tag/release events, so a dispatch can never prove publishing.
- **`.github/workflows/release.yml` — the release lane.** It has its own PyPI
  upload step on tag pushes, marked `continue-on-error: true` so the GitHub
  release is never held hostage by the registry. It authenticates with the same
  repository secret. If both workflows upload the same version, PyPI accepts the
  first and rejects the second with `file already exists` (PyPI files are
  immutable). The publisher passes `skip-existing: true`, so whoever loses that
  race reports the existing files as skipped instead of failing, and the release
  lane ignores its own copy too — the release completes either way.

When you diagnose a failed publish, read the workflow **at the tag you are
diagnosing**, not the copy on `main`. Tag `v1.2.1` still carries the older
OIDC-only form, and its run (41796222963) failed with `invalid-publisher` while
`main` had already switched to the token. Diagnosing a tagged run against `main`
points at the wrong credential.

## Guards that run before anything is uploaded

The `build` job fails the run — before any upload — when:

- `pyproject.toml`'s `[project].version` cannot be read;
- the ref is a `v*.*.*` tag whose name is not exactly `v<version>`, which
  would otherwise publish a version the tag does not name;
- the publish step carries a `password:` input (the token path) and the
  `PYPI_API_TOKEN` secret is not readable by the workflow. The check reads this
  workflow file, so the trusted-publishing form — no `password:` input — does not
  demand a secret.

It also writes the version, the triggering event, the ref and the secret's
presence (never its value) to the job summary, together with what pypi.org
currently serves for that version. That step runs on `workflow_dispatch` too, so
a dispatch is a real preflight of the release identity even though it never
publishes.

## Credential: the PYPI_API_TOKEN secret

The publisher authenticates with the `PYPI_API_TOKEN` repository secret — a PyPI
API token scoped to the `xpst` project. The secret value is never quoted in
documentation; only its name is public.

- It is already configured (`TysAIs/xPST` → **Settings** → **Secrets and
  variables** → **Actions**), so no maintainer step is needed to publish.
- The token path needs **no** `id-token: write` permission and **no**
  `environment:` on the publish job, and `publish-pypi.yml` declares neither.
  The repository does have a `pypi` environment (with no protection rules), but
  the token path does not use it, so nothing waits for a human approval click.
- Rotating the token is the only recurring chore, and only if it is revoked:
  issue a new project-scoped token on PyPI and replace the secret.

## Alternative: trusted publishing (OIDC)

PyPI Trusted Publishing stores no long-lived secret, but it needs a one-time,
account-scoped registration in the PyPI web UI before any publish can succeed.
Register a **pending publisher** at **Your projects** → **Publishing** → **Add a
pending publisher** with exactly these claims:

| Field | Value |
| --- | --- |
| PyPI project name | `xpst` |
| Owner | `TysAIs` |
| Repository name | `xPST` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

Until that registration exists, an OIDC publish fails with `invalid-publisher`
("Publisher with matching claims was not found") — which is exactly why tag
`v1.2.1` failed before the token path was wired up.

Switching to trusted publishing later means, in one commit:

1. Register the pending publisher above on PyPI.
2. Re-add `id-token: write` to the workflow-level `permissions:` and to the
   publish job's `permissions:`.
3. Re-add `environment: pypi` to the publish job.
4. Drop the `password:` input from the publish step, and make the same change in
   the `release.yml` upload step.

The workflow file carries the same note next to the publish step, and
`tests/test_pypi_workflow.py` fails if the workflow and this document disagree on
the credential, the `environment`/`id-token` requirements, or the
`workflow_dispatch` behaviour.

## Release procedure

1. Update the package version in `pyproject.toml` (and every other version
   surface pinned by `tests/test_tauri_production_assembly.py`) so it matches
   the release tag — version `1.2.2` uses tag `v1.2.2`. A version can be
   published only once: PyPI versions are immutable and cannot be reused,
   re-uploaded or deleted-and-replaced.
2. Merge the version change to `main`. Publish from a commit on `main`, never
   from a dirty tree or a branch.
3. Push the version tag, or publish a GitHub release for the existing tag:

   ```bash
   git tag v1.2.2
   git push origin v1.2.2
   ```

   The publish job runs on version-tag pushes (`v*.*.*`) and on published
   GitHub releases. A `workflow_dispatch` run only builds and validates.
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

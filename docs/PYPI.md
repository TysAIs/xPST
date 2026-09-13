# PyPI publication

xPST is published to PyPI by GitHub Actions using PyPI Trusted Publishing. The
workflow uses GitHub's OIDC identity token; no PyPI API token or password is
stored in the repository.

## One-time maintainer setup

A repository maintainer must complete these steps once:

1. Sign in to [PyPI](https://pypi.org/) and create or claim the project named
   `xpst`. Because the project is not published yet, use PyPI's pending
   publisher flow if it is offered instead of uploading a file manually.
2. In the PyPI project settings, add a **Trusted Publisher** with exactly:
   - **Owner:** `TysAIs`
   - **Repository:** `xPST`
   - **Workflow name:** `publish-pypi.yml`
   - **Environment name:** `pypi`
3. In GitHub, open `TysAIs/xPST` → **Settings** → **Environments**, create an
   environment named `pypi`, and configure any required reviewer protection
   desired for publication. The workflow's publish job references this exact
   environment.
4. Merge the pull request containing this workflow. Do not add a
   `PYPI_API_TOKEN`; Trusted Publishing is the only credential path in this
   workflow.

The first publication still requires the maintainer to authorize the PyPI
publisher setup and, if configured, approve the protected `pypi` environment.

## Release procedure

1. Update the package version in `pyproject.toml` and make sure it matches the
   release tag (for example, version `1.2.0` uses tag `v1.2.0`).
2. Merge the version change to `main`.
3. Push a version tag, or publish a GitHub release for that tag:

   ```bash
   git tag v1.2.0
   git push origin v1.2.0
   ```

   The workflow runs on version-tag pushes and on published GitHub releases.
   It builds both an sdist and a wheel, validates both with `twine check`, and
   then publishes them with the OIDC token.
4. If the `pypi` environment requires approval, approve the waiting GitHub
   Actions job. A manual `workflow_dispatch` runs the build and validation, but
   its publish job is intentionally skipped; only a version tag or published
   release can publish.
5. Verify the registry and the installed package after the job succeeds:

   ```bash
   python -m pip install --upgrade xpst
   python -m xpst --version
   curl -s https://pypi.org/pypi/xpst/json
   ```

Do not reuse a version already present on PyPI. PyPI releases are immutable;
correct a bad release by incrementing the version and publishing again.

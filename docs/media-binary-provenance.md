# Media binary provenance (ffmpeg, ffprobe, yt-dlp)

The desktop app bundles ffmpeg, ffprobe and yt-dlp. Those binaries are not part
of this repository, so a release is only as trustworthy as the process that
fetches them. This document describes that process and how to keep it honest.

## The rule

**Every bundled media binary comes from an immutable url and its bytes must
match a SHA-256 that is committed in `scripts/media-binaries.lock`.**

- Immutable url: a versioned release asset (`.../releases/download/<tag>/<file>`),
  never `latest`, never `getrelease`, never a mirror's "current build" path.
- A download whose bytes do not hash to the locked value is **refused and fails
  the build** (`MEDIA BINARY PROVENANCE MISMATCH`). It is never unpacked, never
  installed, and never silently replaced by a different binary.
- An unverified local binary can never reach a release: the PATH fallback exists
  only behind `XPST_ALLOW_UNPINNED_MEDIA=1` (local development), records
  `UNPINNED` in the provenance file, and the release lane fails if it sees an
  `UNPINNED` entry.

Why so strict: the previous fetch step used mirrors such as
`osxexperts.net/ffmpeg6arm.zip`, `gyan.dev/.../ffmpeg-release-essentials.zip`,
`johnvansickle.com/.../ffmpeg-release-amd64-static.tar.xz` and
`yt-dlp/releases/latest/download/yt-dlp`. All of those are moving targets. They
made the lane flaky (the macOS lane died on a truncated download and later
bundled a *different* binary than the previous release), and a lane that ships
different bytes every run cannot be reproduced or audited.

## Lock file format

`scripts/media-binaries.lock`, tab separated, `#` comments allowed:

| column | meaning |
| --- | --- |
| platform | `macos-arm64`, `macos-x64`, `linux-x64`, `linux-arm64`, `win-x64` |
| artifact | `ffmpeg`, `ffprobe`, `yt-dlp` (Windows installs `<artifact>.exe`) |
| kind | `raw` (the download *is* the binary), `zip`, `tar.xz` |
| member | path inside the archive (`-` when `kind` is `raw`) |
| sha256 | lowercase hex digest of the downloaded file |
| url | immutable download url |

Several rows for the same `(platform, artifact)` are candidate sources, tried in
file order: the first one that downloads *and* passes its checksum wins. Windows
and Linux have a second pinned source (a date-stamped BtbN/FFmpeg-Builds
autobuild), so one unavailable mirror cannot fail a lane while the artifact
stays pinned. macOS has a single pinned source on purpose: the alternatives
(osxexperts, evermeet) publish no stable checksum, and an unverifiable fallback
is worse than a red lane.

Current pins:

| artifact | primary | secondary |
| --- | --- | --- |
| ffmpeg / ffprobe (macOS, Linux, Windows) | `eugeneware/ffmpeg-static` tag `b6.1.1` (ffmpeg 6.0) | BtbN `autobuild-2026-09-14-13-17` (Windows, Linux) |
| yt-dlp | `yt-dlp/yt-dlp` tag `2026.08.19` | — |

## What a run records

`scripts/fetch-media-binaries.sh <platform>` writes
`src-tauri/binaries/PROVENANCE.txt`, one line per artifact:

```
pinned  macos-arm64  ffmpeg  sha256=<locked>  installed_sha256=<on disk>  bytes=<n>  url=<immutable url>
```

The release lane prints that file, appends it to the job summary, uploads it as
a release asset (`media-binaries-PROVENANCE-<target>.txt`) and fails the build if
it is missing, empty, has no entry for ffmpeg/ffprobe/yt-dlp, or contains an
`UNPINNED` line. So a published release can always state exactly which bytes it
shipped.

The macOS/Linux x64 yt-dlp asset is a python-zipapp whose shebang is rewritten
when the host python3 is older than 3.10. That rewrite is recorded
(`shebang_rewritten=1`) with the hash of the rewritten file, so the record still
describes what is on disk.

## Verifying or re-pinning

```
# does every pinned url still serve the pinned bytes?
scripts/update-media-binaries-lock.py --check
```

The check asks the GitHub release API for each asset's digest and, for yt-dlp
rows, additionally cross-checks the release's own `SHA2-256SUMS` asset. Two
independent sources must agree. Exit code 1 means drift: a release asset changed
under a pinned tag or disappeared.

To move to a newer upstream release, edit the urls in the lock, then:

```
scripts/update-media-binaries-lock.py --write   # re-record digests from upstream
scripts/update-media-binaries-lock.py --check   # confirm
```

Re-pinning is a deliberate, reviewable commit: the diff shows both the new url
and the new hash. Do not hand-edit hashes.

## Notes

- `scripts/fetch-media-binaries.sh` still retries each download (curl
  `--retry --retry-all-errors`, resume, two rounds) and still reports every
  candidate it tried when a fetch fails - robustness at the transport layer,
  pinning at the provenance layer.
- The lock file is intentionally not a JSON/YAML document: the fetch script
  runs on macOS, Linux and Git Bash on Windows without `jq` or a JSON parser.
- Adding a platform means adding rows for all three artifacts; the static tests
  in `tests/test_fetch_media_binaries_script.py` fail if a platform/artifact
  combination is missing, if a digest is not 64 hex characters, or if a url is
  not an immutable release asset.

# Media binary provenance (yt-dlp)

The desktop bundle ships one media binary, `yt-dlp`. The engine's other media
tools — ffmpeg/ffprobe — are **not** bundled; they are resolved at runtime, and
their own pins live in `src/xpst/media/binaries.py` (see *ffmpeg is not bundled*
below). A release is only as trustworthy as the process that fetches what it
does ship, so this document describes that process and how to keep it honest.

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
- Nothing here is fatal to the lane when a fetch simply fails: the engine's
  bundled `yt_dlp` Python module is the primary path, so `scripts/fetch-media-binaries.sh`
  reports loudly and exits `0`. What the lane refuses is *unverified* bytes, not
  *absent* ones.

Why so strict: the previous fetch step used mirrors such as
`osxexperts.net/ffmpeg6arm.zip`, `gyan.dev/.../ffmpeg-release-essentials.zip`,
`johnvansickle.com/.../ffmpeg-release-amd64-static.tar.xz` and
`yt-dlp/releases/latest/download/yt-dlp`. All of those are moving targets. They
made the lane flaky (the macOS lane died on a truncated download and later
bundled a *different* binary than the previous release), and a lane that ships
different bytes every run cannot be reproduced or audited.

## ffmpeg is not bundled

ffmpeg + ffprobe were 87 MB of a 192 MB app, and the flaky mirror behind that
download is what kept the macOS release lane red. The bundle no longer carries
them at all: the engine resolves

`XPST_FFMPEG_PATH` > a system install > a pinned, checksum-verified copy fetched
on first use into `~/.xpst/bin`

(see `src/xpst/media/binaries.py`, surfaced as `xpst media fetch`). A machine
that already has ffmpeg downloads nothing. Those runtime downloads are pinned
and checksum-verified too, by that module's own constants — which is why the
lock file below carries **no ffmpeg rows**: it describes bundle inputs only, and
nothing in it should name a binary the lane does not ship.

## Lock file format

`scripts/media-binaries.lock`, tab separated, `#` comments allowed:

| column | meaning |
| --- | --- |
| platform | `macos-arm64`, `macos-x64`, `linux-x64`, `linux-arm64`, `win-x64` |
| artifact | `yt-dlp` (Windows installs `<artifact>.exe`) |
| kind | `raw` (the download *is* the binary), `zip`, `tar.xz` |
| member | path inside the archive (`-` when `kind` is `raw`) |
| sha256 | lowercase hex digest of the downloaded file |
| url | immutable download url |

Several rows for the same `(platform, artifact)` are candidate sources, tried in
file order: the first one that downloads *and* passes its checksum wins, so a
single unavailable mirror cannot fail a lane while the artifact stays pinned. The
`kind`/`member` columns and the archive-extraction path are kept even though
yt-dlp is a raw download: a future pinned artifact inside a `.zip`/`.tar.xz`
needs no new machinery.

Current pin:

| artifact | primary |
| --- | --- |
| yt-dlp | `yt-dlp/yt-dlp` tag `2026.08.19` |

## What a run records

`scripts/fetch-media-binaries.sh <platform>` writes
`src-tauri/binaries/PROVENANCE.txt`, one line per installed artifact:

```
pinned  macos-arm64  yt-dlp  sha256=<locked>  installed_sha256=<on disk>  bytes=<n>  url=<immutable url>
```

The release lane prints that file, appends it to the job summary, uploads it as
a release asset (`media-binaries-PROVENANCE-<target>.txt`) and runs
`scripts/verify-media-binaries-provenance.sh` over it. That checker fails the
build if the record contains an `UNPINNED` line, if a row is malformed, or if a
binary is present in the bundle inputs with no pinned row — and deliberately does
not require yt-dlp (or ffmpeg) to be present, because neither absence is fatal.

The macOS/Linux x64 yt-dlp asset is a python-zipapp whose shebang is rewritten
when the host python3 is older than 3.10. That rewrite is recorded
(`shebang_rewritten=1`) with the hash of the rewritten file, so the record still
describes what is on disk. The row is edited in place, so an `UNPINNED` local
fallback stays `UNPINNED` instead of being laundered into a pinned-looking row.

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
- Adding a platform means adding a row for the bundled artifact; the static tests
  in `tests/test_fetch_media_binaries_script.py` fail if a platform is missing,
  if a digest is not 64 hex characters, or if a url is not an immutable release
  asset. The same file fails if an ffmpeg mirror is referenced again.
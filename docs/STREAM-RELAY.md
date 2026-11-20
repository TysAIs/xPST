# Live-stream relay

**Status**: shipped — registry, encrypted key store, relay-config generator, and
ingest-key *fetching* through the platform APIs, with a read-only live-status
read for Twitch/Kick. Vendor rows and endpoints re-checked 2026-11-16 against
the vendor documentation each row cites.

xPST does not re-encode video and does not proxy streams through a server it
operates. The relay is a **local** arrangement: an encoder (OBS, or anything that
can push RTMP) sends **one** stream to a local MediaMTX process, and MediaMTX
forwards the same bytes to every configured destination. That is the whole
feature — one upload's worth of bandwidth instead of N, and no transcoding
generation loss.

MediaMTX is MIT-licensed and runs as an external child process, on the same
footing as `ffmpeg` and `yt-dlp` under DIRECTION D-3. It is not vendored, not
linked, and not bundled by this repository.

## What this repository owns

| Piece | Where | What it decides |
|---|---|---|
| Ingest-path registry | `src/xpst/stream/destinations.py` | which platforms can hand an app a key (`api`), which are per-session copy/paste (`manual`), and which have no live ingest at all (`unsupported`) |
| Encrypted key store | `src/xpst/stream/keys.py` | stream keys live in the same encrypted credential store as cookies and tokens, never in `config.yaml`, never in output |
| Key fetching | `src/xpst/stream/ingest.py` | asks each vendor's documented endpoint for this account's key; refuses with a code and a next step when it cannot |
| App credentials | `src/xpst/stream/ingest.py` | the free provider-app credentials (Twitch/Kick) that make a fetch possible, stored encrypted, masked on every read |
| Relay config generator | `src/xpst/stream/relay.py` | the MediaMTX YAML: one `forward:` entry per stored destination, refusals instead of silent drops |
| Surfaces | `xpst stream {destinations,fetch-key,app-credentials,live,set-key,clear-key,relay-config}`, MCP `xpst_stream`, `GET /api/stream` | one payload (`xpst.stream.ingest.stream_report`) served to humans, agents and the UI |

## The matrix

The classification of record is
[PLATFORM-CAPABILITY-MATRIX.md](PLATFORM-CAPABILITY-MATRIX.md) §2–§3, which
`tests/test_platform_capability_matrix.py` asserts against
`xpst.stream.destinations`. This table is the operator view of the same registry —
how a key arrives and what has actually been proved — not a second source of truth.

| Platform | Ingest path | How the key arrives | Proven live? |
|---|---|---|---|
| YouTube Live | `api` — `liveStreams.insert` | OAuth on the destination channel; `cdn.ingestionInfo.ingestionAddress` + `streamName`. Creating the resource does **not** start or announce a broadcast | **call performed live** — the request authenticated and was correctly formed, and YouTube answered `403 The user is not enabled for live streaming`: the channel has no live access yet. Enabling it (phone verification at youtube.com/features, then up to 24h) is a channel setting, reported as `live_not_enabled` |
| Twitch | `api` — `GET helix/streams/key` | **user** access token with `channel:read:stream_key`, plus the broadcaster id. An app access token cannot read a stream key; Twitch answers `403` for accounts not permitted to stream | no account credentials exist on the dev host — every branch is proved with `httpx.MockTransport` |
| Kick | `api` — `GET public/v1/channels` | user token with `streamkey:read`; the key arrives as `data[].stream.key` with the ingest URL beside it in `data[].stream.url`. Kick publishes **no** standalone stream-key endpoint | **documented, never exercised** — no Kick app credentials on any host we run |
| Facebook Page Live | `api` — `POST /{page-id}/live_videos` | Page-scoped token with `published=false`, so nothing appears on the Page; `secure_stream_url`'s last path segment is the key. A personal profile cannot go live through the API | no — Facebook is disabled here by the configured Meta pause |
| Instagram Live | `manual` | Live Producer issues a **per-session** server URL + key; re-paste before every stream | n/a — no endpoint exists |
| TikTok LIVE | `manual` | LIVE Center issues a per-session key; no documented endpoint hands one to an app | n/a |
| Rumble Live | `manual` | studio RTMP ingest, copy/paste | n/a |
| X / Threads / Bluesky | `unsupported` | no live ingest for third-party apps | n/a |

Refusals carried as data (`SIMULCAST_REFUSALS`): **TikTok Shop prohibits
simulcasting the same live elsewhere**, and **Twitch forbids redirecting viewers
to another platform from a stream**. A surface that offers either combination is
wrong, not flexible.

## Operator flow

```bash
# 1. See what each platform needs, whether a key is stored, and whether xPST
#    holds the credentials to fetch one itself.
xpst stream destinations

# 2. If a platform says app credentials are missing, follow what it prints:
#    register the free app, then store the client id + user access token.
xpst stream app-credentials twitch          # state + the exact registration steps
xpst stream app-credentials twitch --client-id … --user-id …   # token prompted, no echo

# 3. Ask the platform for this account's ingest credentials.
xpst stream fetch-key youtube --title "Relay test"
xpst stream fetch-key twitch

# 4. Write the relay config (0600, never printed back in summary form).
xpst stream relay-config --output ~/.xpst/mediamtx.yml

# 5. Run the relay (MediaMTX, ≥ v1.20 for the native forward list).
mediamtx ~/.xpst/mediamtx.yml

# 6. Push one stream from the encoder.
#    OBS → Server: rtmp://127.0.0.1:1935/xpst   Stream key: (anything non-empty)

# 7. Who is live right now (only platforms with credentials answer).
xpst stream live
```

Manual platforms keep their handoff: a key from Instagram, TikTok or Rumble is
per session, so it is stored with `xpst stream set-key <platform>` and re-pasted
before every stream. `fetch-key` refuses those with the page to copy from.

## Refusal codes (agents branch on these, never on prose)

| `code` | Meaning | What the payload carries |
|---|---|---|
| `credentials_missing` | xPST has nothing to authenticate with | `missing` field names, `action` with the registration step, `docs_url` |
| `manual_required` | the platform has no endpoint by design | `action` naming the page (Live Producer / LIVE Center / studio) |
| `not_supported` | no live ingest for this account type | the registry note |
| `platform_error` | the vendor answered an error status | `status`, redacted vendor text, an `action` for 401/403 |
| `invalid_response` | the vendor answered something unusable | which field was missing |
| `live_not_enabled` | the credential is fine; the *account* has no live access (measured: YouTube 403 on this channel) | `action` naming the platform setting to change |
| `network_error` | the call did not reach the vendor | the exception class, never the URL's credentials |

`xpst stream destinations --json` reports the same verdicts without any network
call: each API row carries a `fetch` block (`credentials_present`, `missing`,
`action`, `registration_url`, `scopes`), and the top level lists `fetchable` and
`missing_credentials`.

## Safety properties the code enforces

- A stream key never appears in `repr()`, JSON output, a summary, or an
  exception message; `RelayDestination.masked()` is the only serialisable view,
  and `FetchedKey`/`AppCredentials` keep their secrets out of `repr`.
- Vendor error text is redacted against the credential before it is echoed, so a
  provider that quotes the token back cannot leak it into a log.
- The generated file is created `0600` before any content is written.
- The relay exposes **only** RTMP ingest with forwarding: RTSP, HLS, WebRTC, SRT,
  MoQ and the MediaMTX HTTP API are switched off, so the relay is not an
  accidental public server and cannot collide with another instance's default
  ports.
- Two destinations for the same platform, an unknown platform, a non-RTMP ingest
  URL, a key containing whitespace or `#`, and an unusable relay path name are
  all **refusals**, because a relay that quietly drops a destination makes the
  operator believe they are live somewhere they are not.
- `unsupported` platforms cannot be stored at all.
- Fetching creates nothing visible: YouTube gets a *stream resource* (never a
  broadcast), Facebook an *unpublished* live video. No spend, no posting.
- A platform without credentials is reported as `unavailable`
  (`credentials_missing`) on the live-status surface — never omitted and never
  rendered as "offline".

## Evidence

- `tests/test_stream_ingest.py` (37 tests) drives all four vendor shapes through
  `httpx.MockTransport` / a fake YouTube service: the request URL, the headers,
  the request body (`published=false` for Facebook, `isReusable=false` for
  YouTube), the key extraction, and every refusal code. It also asserts that the
  registry's endpoint text matches the endpoint the code calls — the Kick row
  used to name `/public/v1/stream/key`, which is not in Kick's published index.
- The live YouTube fetch was performed against the connected channel on
  2026-11-16. YouTube accepted the OAuth credential and answered
  `403 The user is not enabled for live streaming` — no stream resource was
  created, nothing was broadcast, announced or published, and no key was stored.
  That measurement is why `live_not_enabled` exists as its own refusal code
  rather than being folded into a generic `platform_error`, and why the
  operator action it returns says "enable live on the channel", not
  "re-authorise".

## Not built yet (deliberately)

1. **Twitch/Kick live fetches.** Both need free app credentials that do not
   exist on any host we run; the code paths are proved at the transport boundary
   and refuse with the registration step. When someone with an account runs
   `xpst stream fetch-key kick`, this document gains a real data point.
2. **Chat fan-in** — built: see [CHAT-MIXING.md](CHAT-MIXING.md). Twitch is read
   anonymously over IRC, YouTube through `liveChatMessages.list`, Kick through its
   undocumented realtime socket (whose public Pusher path is dead — the document
   says what replaced it). Read-only, one merged timeline, no send path.
3. **Multi-user hosting.** Single-tenant by design: serving other people's
   accounts means Instagram Advanced Access, the TikTok app audit and X credits,
   which are approvals, not engineering.
